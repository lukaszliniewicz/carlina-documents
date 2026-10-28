"""Build source-free semantic export requests for document-block projects.

The block worker owns translation and target activation.  This module owns the
last, deliberately small trust boundary before a Document Processor semantic
export provider is called: it rehydrates one immutable source plan, renders the
currently persisted target through the verified target renderer, and binds the
result to source, target, operation, and provider witnesses.

There is no provider call, filesystem access, or upload lookup here.  Opaque
resource references remain in the request for every format; the owning semantic
export provider resolves them and makes HTML resources standalone.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from bs4 import BeautifulSoup

from carlina.core.epub_cover import normalize_epub_cover
from carlina.core.epub_export import (
    EpubExportContractError,
    normalize_epub_options,
    normalize_epub_image_mode,
    semantic_epub_review_sha256,
)
from carlina.core.semantic_export import (
    SEMANTIC_EXPORT_FORMATS,
    SemanticExportContractError,
    SemanticExportRequest,
    SemanticExportResource,
    default_include_source_toc,
    parse_opaque_resource_path,
)
from carlina.core.semantic_html import SAFE_LANGUAGE_RE

from .block_contract import BlockContractError
from .block_persistence import rehydrate_source_plan
from .export_metadata import ExportMetadataError, apply_export_metadata
from .export_metadata import normalize_export_metadata as _normalize_export_metadata
from .export_presentation import present_export
from .models import TranslationSourcePlan as TranslationSourcePlanRecord
from .numbering_sync import numbering_revision
from .target_persistence import render_persisted_target_document

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_PUBLICATION_KEYS = frozenset(
    {
        "title",
        "subtitle",
        "language",
        "contributors",
        "author",
        "publication_type",
        "cover_image_id",
        "publisher",
        "publication_date",
        "rights",
        "isbn",
        "issn",
        "identifiers",
        "source",
        "description",
        "subjects",
        "journal_or_series",
        "volume",
        "issue",
    }
)
_NOTE_ARRANGEMENT_PLACEMENTS = frozenset(
    {
        "page_footnotes",
        "chapter_endnotes",
        "document_endnotes",
        "mixed",
        "none",
        "unknown",
    }
)
_NOTE_ARRANGEMENT_NUMBERING = frozenset(
    {"document", "chapter", "section", "page", "mixed", "unknown"}
)
_NOTE_ARRANGEMENT_MAX_PAGES = 32
_NOTE_ARRANGEMENT_MAX_EVIDENCE_CHARS = 800
_NOTE_ARRANGEMENT_MAX_EVIDENCE_BYTES = 4096
_INCOMPLETE_RENDER_CODES = frozenset(
    {
        "target-document-block-cardinality",
        "target-document-block-missing",
        "target-active-cardinality",
        "target-active-pair",
        "target-render-project",
    }
)

SEMANTIC_EXPORT_PROFILE_ID = "semantic-default"
SEMANTIC_EXPORT_PROFILE_VERSION = "1"

_SEMANTIC_PREVIEW_STYLE_ID = "carlina-semantic-preview"
_SEMANTIC_PREVIEW_POLICY_ID = "carlina-semantic-preview-policy"
_SEMANTIC_PREVIEW_POLICY = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'"
)
_SEMANTIC_PREVIEW_STYLES = """
:root {
  color-scheme: light;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 17px;
  line-height: 1.68;
  color: #302d29;
  background: #e9e4db;
}
* { box-sizing: border-box; }
html { min-height: 100%; background: #e9e4db; }
body {
  width: min(calc(100% - 2rem), 54rem);
  min-height: 100vh;
  margin: 0 auto;
  padding: clamp(2rem, 5vw, 4.5rem) clamp(1.35rem, 6vw, 5rem) 5rem;
  color: #302d29;
  background: #fffdf8;
  overflow-wrap: anywhere;
  text-rendering: optimizeLegibility;
}
main { display: block; }
h1, h2, h3, h4, h5, h6 {
  margin: 1.6em 0 .55em;
  color: #27231f;
  font-weight: 500;
  line-height: 1.16;
  text-wrap: balance;
}
h1 { font-size: clamp(2rem, 5vw, 3.15rem); }
h2 { font-size: clamp(1.55rem, 3.4vw, 2.15rem); }
h3 { font-size: clamp(1.25rem, 2.5vw, 1.6rem); }
h4, h5, h6 { font-size: 1.05rem; font-weight: 650; }
p, ul, ol, dl, blockquote, pre, table, figure { margin: .9rem 0; }
p { text-wrap: pretty; }
ul, ol { padding-inline-start: 1.55rem; }
li + li { margin-top: .28rem; }
a { color: inherit; text-decoration-color: #89786c; text-underline-offset: .15em; }
blockquote {
  margin-inline: 0;
  padding-inline: 1rem;
  border-inline-start: 2px solid #b9aea1;
  color: #4d4842;
}
figure { margin-block: 2rem; }
figcaption { margin-top: .65rem; color: #675f56; font-size: .84rem; line-height: 1.5; }
img, svg, video { display: block; max-width: 100%; height: auto; margin-inline: auto; }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th, td { padding: .55rem .65rem; border: 1px solid #d0c8bc; text-align: start; vertical-align: top; }
th { background: #f1eee7; }
pre { max-width: 100%; overflow-x: auto; padding: .85rem; border: 1px solid #d0c8bc; white-space: pre-wrap; }
code, kbd, samp, var { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .9em; }
hr { margin: 2.5rem 0; border: 0; border-top: 1px solid #d0c8bc; }
.pagebreak, [role="doc-pagebreak"] {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  margin: -1px !important;
  padding: 0 !important;
  overflow: hidden !important;
  clip: rect(0 0 0 0) !important;
  white-space: nowrap !important;
}
.footnotes, [role="doc-endnotes"] { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid #d0c8bc; font-size: .86rem; }
.footnote-ref, [role="doc-noteref"] { font-size: .78em; vertical-align: super; }
@media (max-width: 40rem) {
  :root { font-size: 16px; }
  body { width: 100%; padding: 1.5rem 1.15rem 3rem; }
}
@media print {
  html, body { background: #fff; }
  body { width: auto; padding: 0; }
  .pagebreak, [role="doc-pagebreak"] { break-before: page; }
}
""".strip()


class BlockExportError(ValueError):
    """Bounded failure at the Translator semantic-export boundary."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", code):
            code = "block-export-invalid"
        self.code = code
        super().__init__(code)


def normalize_epub_review_hash(value: object) -> str:
    """Normalize one preflight witness without retaining any source content."""

    if not isinstance(value, str):
        raise BlockExportError("epub-review-hash-invalid")
    normalized = value.strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise BlockExportError("epub-review-hash-invalid")
    return normalized


def semantic_export_review_hash(
    request_snapshot: Mapping[str, object],
) -> str:
    """Hash one serialized request's normalized presentation inputs, not its HTML."""

    if "semantic_html" not in request_snapshot:
        raise BlockExportError("semantic-export-request")
    return semantic_epub_review_sha256(request_snapshot)


def normalize_export_metadata(value: object) -> dict[str, object]:
    """Validate and copy the optional per-export metadata overrides."""
    try:
        return _normalize_export_metadata(value)
    except ExportMetadataError as exc:
        raise BlockExportError(exc.code) from exc


def merge_project_export_metadata(
    project: object,
    overrides: Mapping[str, object] | None = None,
    *,
    use_uploaded_cover: bool = True,
) -> dict[str, object]:
    """Merge saved and per-export metadata without capturing or writing covers."""

    settings = getattr(project, "settings", {}) or {}
    if not isinstance(settings, Mapping):
        raise BlockExportError("export-metadata-invalid")
    merged = normalize_export_metadata(settings.get("export_metadata", {}))
    normalized_overrides = normalize_export_metadata(
        {} if overrides is None else overrides
    )
    for key, value in normalized_overrides.items():
        merged[key] = list(value) if isinstance(value, list) else value
    if not use_uploaded_cover:
        merged.pop("cover_upload_path", None)
    return normalize_export_metadata(merged)


def validate_epub_preflight_result(
    value: object,
    *,
    semantic_html_sha256: str,
    epub_options: Mapping[str, object],
    expected_review_hash: str,
) -> dict[str, object]:
    """Validate the cross-add-on preflight response against its exact input."""

    from carlina.core.epub_export import (
        EpubExportContractError,
        epub_value_sha256,
        normalize_epub_options,
        normalize_epub_quality,
    )

    fields = {
        "options",
        "suggested_chapter_level",
        "outline",
        "quality",
        "review_hash",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise BlockExportError("semantic-export-preflight-result")
    try:
        response_options = normalize_epub_options(value["options"])
        expected_options = normalize_epub_options(epub_options)
        quality = normalize_epub_quality(value["quality"])
    except EpubExportContractError:
        raise BlockExportError("semantic-export-preflight-result") from None
    level = value["suggested_chapter_level"]
    outline = value["outline"]
    response_review_hash = value["review_hash"]
    if (
        response_options != expected_options
        or not isinstance(expected_review_hash, str)
        or _SHA256_RE.fullmatch(expected_review_hash) is None
        or not isinstance(response_review_hash, str)
        or _SHA256_RE.fullmatch(response_review_hash) is None
        or response_review_hash != expected_review_hash
        or (level is not None and (type(level) is not int or not 1 <= level <= 6))
        or not isinstance(outline, Mapping)
        or not {"input_sha256", "headings", "tables", "images", "pages"} <= set(outline) <= {"input_sha256", "headings", "tables", "images", "pages", "front_matter"}
        or outline.get("input_sha256") != semantic_html_sha256
        or any(
            not isinstance(outline.get(name), list)
            for name in ("headings", "tables", "images", "pages")
        )
        or quality["input_sha256"] != semantic_html_sha256
        or quality["options_sha256"] != epub_value_sha256(expected_options)
        or quality["profile"] != expected_options["profile"]
    ):
        raise BlockExportError("semantic-export-preflight-result")
    return {
        "options": response_options,
        "suggested_chapter_level": level,
        "outline": dict(outline),
        "quality": quality,
        "review_hash": response_review_hash,
    }


def build_semantic_preview_html(value: str | bytes) -> str:
    """Add one inert, local presentation profile to verified semantic HTML."""

    if isinstance(value, bytes):
        try:
            document = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BlockExportError("semantic-preview-encoding") from exc
    elif isinstance(value, str):
        document = value
    else:
        raise BlockExportError("semantic-preview-document")
    if not document.strip():
        raise BlockExportError("semantic-preview-document")

    soup = BeautifulSoup(document, "html.parser")
    root = soup.find("html")
    body = soup.find("body")
    if root is None or body is None:
        raise BlockExportError("semantic-preview-document")
    head = soup.find("head")
    if head is None:
        head = soup.new_tag("head")
        body.insert_before(head)
    policy = head.find("meta", id=_SEMANTIC_PREVIEW_POLICY_ID)
    if policy is None:
        policy = soup.new_tag("meta", id=_SEMANTIC_PREVIEW_POLICY_ID)
        policy["http-equiv"] = "Content-Security-Policy"
        policy["content"] = _SEMANTIC_PREVIEW_POLICY
        head.insert(0, policy)
    existing = head.find("style", id=_SEMANTIC_PREVIEW_STYLE_ID)
    if existing is None:
        style = soup.new_tag("style", id=_SEMANTIC_PREVIEW_STYLE_ID)
        style.string = _SEMANTIC_PREVIEW_STYLES
        head.append(style)
    return soup.decode(formatter="html")


def _plain_json(value: object, *, depth: int = 0) -> Any:
    """Copy immutable snapshot values into a JSON-compatible plain tree."""

    if depth > 8:
        raise BlockExportError("source-metadata-depth")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise BlockExportError("source-metadata-key")
            result[key] = _plain_json(item, depth=depth + 1)
        return result
    if isinstance(value, (tuple, list)):
        return [_plain_json(item, depth=depth + 1) for item in value]
    raise BlockExportError("source-metadata-value")


def _canonical_json(value: object, code: str) -> str:
    try:
        plain = _plain_json(value)
        return json.dumps(
            plain,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockExportError(code) from exc


def _canonical_sha256(value: object, code: str) -> str:
    return hashlib.sha256(_canonical_json(value, code).encode("utf-8")).hexdigest()


def _safe_metadata_projection(value: object, *, depth: int = 0) -> Any:
    """Project canonical publication metadata without copying raw source data.

    Full source evidence remains in the immutable source plan, and its
    canonical SHA256 witness binds the exact source mapping. Only known root
    publication keys enter provider metadata; unknown raw root fields affect
    the witness without being copied into that projection.
    """

    if depth > 8:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                continue
            if depth == 0 and key not in _SOURCE_PUBLICATION_KEYS:
                continue
            projected = _safe_metadata_projection(item, depth=depth + 1)
            if projected is not None:
                result[key] = projected
        return result
    if isinstance(value, (tuple, list)):
        return [
            projected
            for item in value
            if (projected := _safe_metadata_projection(item, depth=depth + 1))
            is not None
        ]
    return None


def _safe_note_arrangement_projection(value: object) -> dict[str, Any] | None:
    """Project only the closed source note directive used by EPUB packaging."""

    if not isinstance(value, Mapping) or not value:
        return None

    placement = value.get("placement")
    if not isinstance(placement, str) or placement not in _NOTE_ARRANGEMENT_PLACEMENTS:
        placement = "unknown"

    numbering = value.get("numbering")
    if not isinstance(numbering, str) or numbering not in _NOTE_ARRANGEMENT_NUMBERING:
        numbering = "unknown"

    heading_level = value.get("scope_heading_level")
    if type(heading_level) is not int or not 1 <= heading_level <= 6:
        heading_level = None

    raw_pages = value.get("source_pages")
    pages: list[int] = []
    if isinstance(raw_pages, (list, tuple)):
        pages = sorted(
            {
                page
                for page in raw_pages
                if type(page) is int and 0 < page <= 1_000_000_000
            }
        )[:_NOTE_ARRANGEMENT_MAX_PAGES]

    evidence = value.get("evidence")
    projected_evidence = ""
    if isinstance(evidence, str):
        characters: list[str] = []
        encoded_bytes = 0
        for character in evidence.strip():
            if len(characters) >= _NOTE_ARRANGEMENT_MAX_EVIDENCE_CHARS:
                break
            character_bytes = len(character.encode("utf-8"))
            if encoded_bytes + character_bytes > _NOTE_ARRANGEMENT_MAX_EVIDENCE_BYTES:
                break
            characters.append(character)
            encoded_bytes += character_bytes
        projected_evidence = "".join(characters)

    return {
        "placement": placement,
        "numbering": numbering,
        "scope_heading_level": heading_level,
        "source_pages": pages,
        "evidence": projected_evidence,
    }


def _witness(value: Mapping[str, object] | None, *, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BlockExportError(code)
    encoded = _canonical_json(value, code)
    if len(encoded.encode("utf-8")) > 4096:
        raise BlockExportError(code)
    return encoded


def _resource_refs(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    refs: set[str] = set()
    for image in soup.find_all("img"):
        ref = parse_opaque_resource_path(str(image.get("src") or "").strip())
        if ref is not None:
            refs.add(ref)
    return refs


def _source_plan_identity(record: Any, plan: Any) -> tuple[str, str, Any]:
    record_id = getattr(record, "id", None)
    plan_hash = getattr(plan, "plan_hash", None)
    snapshot = getattr(plan, "snapshot", None)
    snapshot_id = getattr(snapshot, "id", None)
    document_id = getattr(snapshot, "document_id", None)
    content_hash = getattr(snapshot, "content_hash", None)
    if (
        not isinstance(record_id, str)
        or not record_id
        or not isinstance(plan_hash, str)
        or not plan_hash
        or not isinstance(snapshot_id, str)
        or snapshot_id != document_id
        or not isinstance(content_hash, str)
        or getattr(record, "plan_hash", plan_hash) != plan_hash
        or getattr(record, "source_content_hash", content_hash) != content_hash
        or getattr(record, "source_document_id", document_id) != document_id
    ):
        raise BlockExportError("source-plan-invalid")
    return record_id, plan_hash, snapshot


def _target_language(record: Any) -> str:
    project = getattr(record, "project", None)
    language = getattr(project, "target_lang", None)
    if not isinstance(language, str) or not SAFE_LANGUAGE_RE.fullmatch(language):
        raise BlockExportError("target-language-invalid")
    return language


def _render_target(
    record: Any,
    renderer: Callable[[Any], str],
) -> str:
    try:
        html = renderer(record)
    except BlockContractError as exc:
        code = (
            "target-incomplete"
            if exc.code in _INCOMPLETE_RENDER_CODES
            else "target-tampered"
        )
        raise BlockExportError(code) from exc
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise BlockExportError("target-tampered") from exc
    if not isinstance(html, str) or not html.strip():
        raise BlockExportError("target-incomplete")
    return html


def build_semantic_export_request(
    source_plan_record: Any,
    *,
    export_format: str,
    include_source_toc: bool | None = None,
    start_h2_on_new_page: bool = False,
    profile_id: str = SEMANTIC_EXPORT_PROFILE_ID,
    profile_version: str = SEMANTIC_EXPORT_PROFILE_VERSION,
    operation_witness: Mapping[str, object] | None = None,
    provider_witness: Mapping[str, object] | None = None,
    export_metadata: Mapping[str, object] | None = None,
    cover_uri: str | None = None,
    epub_cover: object = None,
    epub_options: object = None,
    epub_image_mode: str = "auto",
    renderer: Callable[[Any], str] | None = None,
) -> SemanticExportRequest:
    """Build one exact semantic-export request from a persisted block target.

    ``source_plan_record`` must already be the single verified plan selected by
    the owning operation.  Rehydration verifies the immutable source snapshot;
    the renderer verifies target cardinality, ownership, and target revision
    integrity.  No current source-document lookup is performed here.
    """

    if export_format not in SEMANTIC_EXPORT_FORMATS:
        raise BlockExportError("semantic-export-format")
    try:
        normalize_epub_image_mode(epub_image_mode)
    except EpubExportContractError as exc:
        raise BlockExportError(exc.code) from exc
    if epub_image_mode != "auto" and export_format != "epub":
        raise BlockExportError("semantic-export-epub-image-mode-format")
    try:
        cover_options = normalize_epub_cover(epub_cover)
    except ValueError as exc:
        raise BlockExportError("semantic-export-cover-option") from exc
    if cover_options and (export_format != "epub" or cover_uri):
        raise BlockExportError("semantic-export-cover-option")
    normalized_epub_options = None
    if epub_options is not None:
        try:
            normalized_epub_options = normalize_epub_options(epub_options)
        except EpubExportContractError as exc:
            raise BlockExportError(exc.code) from exc
        if export_format != "epub":
            raise BlockExportError("semantic-export-epub-options-format")
    if include_source_toc is None:
        try:
            include_source_toc = default_include_source_toc(export_format)
        except SemanticExportContractError as exc:
            raise BlockExportError("semantic-export-format") from exc
    if not isinstance(include_source_toc, bool):
        raise BlockExportError("semantic-export-source-toc")
    if type(start_h2_on_new_page) is not bool or (
        start_h2_on_new_page and export_format not in {"docx", "pdf"}
    ):
        raise BlockExportError("semantic-export-start-h2")
    normalized_export_metadata = normalize_export_metadata(
        {} if export_metadata is None else export_metadata
    )

    try:
        plan = rehydrate_source_plan(source_plan_record)
    except (BlockContractError, TypeError, ValueError, AttributeError) as exc:
        raise BlockExportError("source-plan-invalid") from exc
    source_plan_id, plan_hash, snapshot = _source_plan_identity(
        source_plan_record, plan
    )
    target_language = _target_language(source_plan_record)
    html = _render_target(
        source_plan_record,
        renderer if renderer is not None else render_persisted_target_document,
    )

    resources = getattr(snapshot, "resources", ())
    if isinstance(resources, list):
        resources = tuple(resources)
    if not isinstance(resources, tuple) or not all(
        isinstance(resource, SemanticExportResource) for resource in resources
    ):
        raise BlockExportError("source-resources-invalid")
    declared_refs = {resource.ref for resource in resources}
    source_resources = resources
    referenced_refs = _resource_refs(html)
    if referenced_refs != declared_refs:
        raise BlockExportError("target-resource-manifest")
    metadata_source = getattr(snapshot, "metadata", {})
    metadata = _safe_metadata_projection(metadata_source)
    note_arrangement = None
    if export_format == "epub" and isinstance(metadata_source, Mapping):
        note_arrangement = _safe_note_arrangement_projection(
            metadata_source.get("note_arrangement")
        )
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = apply_export_metadata(
        metadata, normalized_export_metadata, target_language
    )
    if (
        normalized_export_metadata.get("cover_upload_path")
        and not cover_uri
        and not cover_options
    ):
        raise BlockExportError("export-cover-unresolved")
    html = present_export(
        html,
        metadata,
        cover_uri=cover_uri,
        omit_cover=bool(cover_options),
        renew_cover_identity=normalized_epub_options is not None,
    )
    if cover_uri or cover_options:
        # Source resources were checked before presentation. Replacing the
        # designated cover may retire its sole reference; other images remain.
        remaining_refs = _resource_refs(html)
        resources = tuple(
            resource for resource in resources if resource.ref in remaining_refs
        )
        metadata.pop("cover_image_id", None)
    if note_arrangement is not None:
        # Keep this source directive separate from editable edition metadata.
        metadata["note_arrangement"] = note_arrangement
    html_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    operation_json = _witness(operation_witness, code="operation-witness")
    provider_json = _witness(provider_witness, code="provider-witness")
    metadata.update(
        {
            "source-plan": {
                "id": source_plan_id,
                "plan-hash": plan_hash,
                "content-hash": snapshot.content_hash,
                "numbering-revision": (
                    numbering_revision(source_plan_record.project_id)
                    if isinstance(source_plan_record, TranslationSourcePlanRecord)
                    else 0
                ),
            },
            "source-snapshot": {
                "id": snapshot.id,
                "document-id": snapshot.document_id,
                "content-hash": snapshot.content_hash,
                "semantic-content-hash": snapshot.semantic_content_hash,
                "filename": snapshot.filename,
                "captured-at": snapshot.captured_at,
                "source-updated-at": snapshot.source_updated_at,
                # Bind the original source manifest even when presentation
                # replaces its cover in the provider-facing resource list.
                "resources-sha256": _canonical_sha256(
                    [resource.to_dict() for resource in source_resources],
                    "source-resources",
                ),
                "resources-count": len(source_resources),
            },
            "source-metadata-sha256": _canonical_sha256(
                metadata_source, "source-metadata"
            ),
            "source-provenance-sha256": _canonical_sha256(
                getattr(snapshot, "provenance", {}), "source-provenance"
            ),
            "target": {
                "language": target_language,
                "semantic-html-sha256": html_hash,
            },
        }
    )
    if cover_options:
        metadata["epub-cover"] = cover_options
        source_pdf_hash = snapshot.provenance.get("source_pdf_sha256")
        if isinstance(source_pdf_hash, str) and re.fullmatch(
            r"[0-9a-f]{64}", source_pdf_hash
        ):
            metadata["source-snapshot"]["pdf-sha256"] = source_pdf_hash
    if operation_json is not None:
        metadata["operation-witness-json"] = operation_json
        metadata["operation-witness-sha256"] = hashlib.sha256(
            operation_json.encode("utf-8")
        ).hexdigest()
    if provider_json is not None:
        metadata["provider-witness-json"] = provider_json
        metadata["provider-witness-sha256"] = hashlib.sha256(
            provider_json.encode("utf-8")
        ).hexdigest()
    try:
        return SemanticExportRequest(
            semantic_html=html,
            semantic_html_sha256=html_hash,
            metadata=metadata,
            resources=resources,
            format=export_format,
            include_source_toc=include_source_toc,
            start_h2_on_new_page=start_h2_on_new_page,
            profile_id=profile_id,
            profile_version=profile_version,
            epub_options=normalized_epub_options,
            epub_image_mode=epub_image_mode,
        )
    except SemanticExportContractError as exc:
        raise BlockExportError("semantic-export-invalid") from exc


__all__ = [
    "SEMANTIC_EXPORT_PROFILE_ID",
    "SEMANTIC_EXPORT_PROFILE_VERSION",
    "BlockExportError",
    "build_semantic_export_request",
    "build_semantic_preview_html",
    "normalize_export_metadata",
]
