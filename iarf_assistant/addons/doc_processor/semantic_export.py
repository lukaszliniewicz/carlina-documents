"""Document Processor implementation of the host semantic-export contract.

The public request contains only an exact semantic target and opaque resources.
This adapter is the sole place that re-associates those references with the
owner-scoped resource index kept by Document Processor.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from sqlalchemy.orm.attributes import flag_modified

from carlina.core.diagram_rendering import project_diagrams_html
from carlina.core.epub_cover import normalize_epub_cover
from carlina.core.export_notes import normalize_export_note_roles
from carlina.core.semantic_export import (
    SemanticExportArtifact,
    SemanticExportRequest,
    SemanticExportResource,
    opaque_resource_path,
    parse_opaque_resource_path,
)

from ...core.upload_paths import resolve_upload_reference, uploads_root
from .semantic_document import serialize_semantic_html, validate_semantic_export

RESOURCE_INDEX_KEY = "semantic_export_resources_v1"
VALIDATION_PROFILE = "document-processor-semantic-export"
VALIDATION_PROFILE_VERSION = "2"
_PRIVATE_RESOURCE_KEYS = {
    "ref",
    "source",
    "media_type",
    "filename",
    "byte_length",
    "sha256",
    "owner_attestation",
}


class SemanticExportError(ValueError):
    """A source-free adapter failure."""


@dataclass(frozen=True, slots=True)
class ExportedSemanticArtifact:
    receipt: SemanticExportArtifact
    data: bytes


def _resource_ref(document_id: str, source: str, digest: str) -> str:
    token = hashlib.sha256(f"{document_id}\0{source}\0{digest}".encode()).hexdigest()
    return f"res_{token}"


def _owner_attestation(*, user_id: int, document_id: str, ref: str, digest: str) -> str:
    # This is an owner-scoped identity witness, not a credential.  It prevents
    # an opaque reference from being substituted across a user/document scope.
    return hashlib.sha256(
        f"doc-processor-resource-v1\0{user_id}\0{document_id}\0{ref}\0{digest}".encode()
    ).hexdigest()


def _media_type(path: Path) -> str | None:
    value, _ = mimetypes.guess_type(path.name)
    return value if value and value.startswith("image/") else None


def _is_local_image_source(source: str) -> bool:
    parsed = urlparse(source)
    return (
        bool(source)
        and not source.startswith("data:")
        and not parsed.scheme
        and not source.startswith("//")
    )


def _document_resource_entries(
    document,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Derive a strict private index and public opaque manifest from one row."""

    html = str(getattr(document, "output_html", "") or "")
    soup = BeautifulSoup(html, "html.parser")
    private_entries: list[dict[str, object]] = []
    source_to_ref: dict[str, str] = {}
    for image in soup.find_all("img"):
        source = str(image.get("src") or "").strip()
        if not _is_local_image_source(source):
            continue
        try:
            path = resolve_upload_reference(
                source, uploads_dir=uploads_root(), require_within_uploads=True
            )
            if path is None or not path.is_file():
                raise OSError("missing")
            owner_root = uploads_root() / str(document.user_id)
            path.resolve().relative_to(owner_root.resolve())
            data = path.read_bytes()
        except OSError, RuntimeError, TypeError, ValueError:
            raise SemanticExportError("semantic-export-resource-unavailable") from None
        media_type = _media_type(path)
        if media_type is None:
            raise SemanticExportError("semantic-export-resource-media")
        digest = hashlib.sha256(data).hexdigest()
        ref = _resource_ref(str(document.id), source, digest)
        attestation = _owner_attestation(
            user_id=int(document.user_id),
            document_id=str(document.id),
            ref=ref,
            digest=digest,
        )
        entry = {
            "ref": ref,
            "source": source,
            "media_type": media_type,
            "filename": path.name,
            "byte_length": len(data),
            "sha256": digest,
            "owner_attestation": attestation,
        }
        if entry not in private_entries:
            private_entries.append(entry)
        source_to_ref[source] = ref
    return private_entries, source_to_ref


def capture_snapshot_resources(document) -> tuple[list[dict[str, object]], str]:
    """Return a public manifest and opaque-URL semantic HTML for one snapshot.

    The private index stays inside Document Processor settings.  It is captured
    before the provider DTO crosses the add-on boundary; a later export checks
    every indexed byte again and fails rather than resolving a mutable upload.
    """

    private_entries, source_to_ref = _document_resource_entries(document)
    original_settings = getattr(document, "settings", None) or {}
    if not isinstance(original_settings, dict):
        raise SemanticExportError("semantic-export-resource-index")
    historical = original_settings.get(RESOURCE_INDEX_KEY, [])
    if not isinstance(historical, list):
        raise SemanticExportError("semantic-export-resource-index")
    merged: list[dict[str, object]] = []
    by_ref: dict[str, dict[str, object]] = {}
    for entry in [*historical, *private_entries]:
        if not isinstance(entry, dict) or set(entry) != _PRIVATE_RESOURCE_KEYS:
            raise SemanticExportError("semantic-export-resource-index")
        copied = dict(entry)
        ref = copied.get("ref")
        if not isinstance(ref, str):
            raise SemanticExportError("semantic-export-resource-index")
        existing = by_ref.get(ref)
        if existing is not None:
            if existing != copied:
                raise SemanticExportError("semantic-export-resource-index")
            continue
        by_ref[ref] = copied
        merged.append(copied)
    if historical != merged:
        settings = dict(original_settings)
        settings[RESOURCE_INDEX_KEY] = merged
        document.settings = settings
        flag_modified(document, "settings")
    if not private_entries:
        # Preserve source bytes exactly when there is nothing to replace.
        return [], str(document.output_html or "")
    soup = BeautifulSoup(str(document.output_html or ""), "html.parser")
    for image in soup.find_all("img"):
        source = str(image.get("src") or "").strip()
        if source in source_to_ref:
            image["src"] = opaque_resource_path(source_to_ref[source])
    public = []
    for entry in private_entries:
        public.append(
            {
                key: entry[key]
                for key in (
                    "ref",
                    "media_type",
                    "filename",
                    "byte_length",
                    "sha256",
                    "owner_attestation",
                )
            }
        )
    return public, serialize_semantic_html(soup)


def _validate_target_resources(request: SemanticExportRequest) -> set[str]:
    soup = BeautifulSoup(request.semantic_html, "html.parser")
    try:
        validate_semantic_export(soup, export_name="Semantic export")
    except ValueError as exc:
        raise SemanticExportError("semantic-export-invalid-html") from exc
    declared = {resource.ref for resource in request.resources}
    referenced: set[str] = set()
    for image in soup.find_all("img"):
        source = str(image.get("src") or "").strip()
        ref = parse_opaque_resource_path(source)
        if ref is not None:
            if not ref or ref not in declared:
                raise SemanticExportError("semantic-export-resource-reference")
            referenced.add(ref)
        elif _is_local_image_source(source):
            # A target must never smuggle an unresolved provider path across
            # the boundary.
            raise SemanticExportError("semantic-export-resource-reference")
        elif source and not source.startswith("data:"):
            raise SemanticExportError("semantic-export-resource-reference")
    if referenced != declared:
        raise SemanticExportError("semantic-export-resource-manifest")
    return referenced


class DocumentResourceResolver:
    """Resolve only exact resources indexed by the owned provider document."""

    def __init__(self, *, user_id: int):
        self.user_id = int(user_id)

    def resolve_pdf_cover(self, request: SemanticExportRequest, page: int) -> bytes:
        from .cover_source import owned_pdf_source
        from .epub_cover import render_pdf_cover
        from .models import ProcessedDocument

        snapshot = request.metadata.get("source-snapshot")
        if not isinstance(snapshot, Mapping):
            raise SemanticExportError("semantic-export-cover-source")
        document = ProcessedDocument.query.filter_by(
            id=snapshot.get("document-id"), user_id=self.user_id
        ).first()
        if document is None:
            raise SemanticExportError("semantic-export-cover-source")
        # Compare the same opaque-resource projection captured by the source
        # snapshot, without writing an index or changing its frozen target.
        _entries, sources = _document_resource_entries(document)
        html = str(document.output_html or "")
        if sources:
            soup = BeautifulSoup(html, "html.parser")
            for image in soup.find_all("img"):
                source = str(image.get("src") or "").strip()
                if source in sources:
                    image["src"] = opaque_resource_path(sources[source])
            html = serialize_semantic_html(soup)
        if hashlib.sha256(html.encode("utf-8")).hexdigest() != snapshot.get(
            "content-hash"
        ):
            raise SemanticExportError("semantic-export-cover-source-changed")
        return render_pdf_cover(
            owned_pdf_source(document, user_id=self.user_id),
            page,
            expected_sha256=snapshot.get("pdf-sha256"),
            crop=cast(
                list[float] | None,
                normalize_epub_cover(request.metadata.get("epub-cover")).get("crop"),
            ),
        )

    def resolve(self, resource: SemanticExportResource) -> bytes:
        from .models import ProcessedDocument

        documents = ProcessedDocument.query.filter_by(user_id=self.user_id).all()
        for document in documents:
            entries = (document.settings or {}).get(RESOURCE_INDEX_KEY)
            if not isinstance(entries, list):
                # A legacy source snapshot may have been handed off before a
                # request transaction committed the private index. Rebuild it
                # from the owned current row only; every opaque identity and
                # every byte is still verified below, so this never turns into
                # a mutable upload-path fallback.
                try:
                    entries, _unused_sources = _document_resource_entries(document)
                except SemanticExportError:
                    continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("ref") != resource.ref:
                    continue
                expected = {
                    key: entry.get(key)
                    for key in (
                        "ref",
                        "media_type",
                        "filename",
                        "byte_length",
                        "sha256",
                        "owner_attestation",
                    )
                }
                if expected != resource.to_dict():
                    raise SemanticExportError("semantic-export-resource-attestation")
                if entry.get("owner_attestation") != _owner_attestation(
                    user_id=self.user_id,
                    document_id=str(document.id),
                    ref=resource.ref,
                    digest=resource.sha256,
                ):
                    raise SemanticExportError("semantic-export-resource-attestation")
                source = entry.get("source")
                if not isinstance(source, str):
                    raise SemanticExportError("semantic-export-resource-index")
                try:
                    path = resolve_upload_reference(
                        source, uploads_dir=uploads_root(), require_within_uploads=True
                    )
                    if path is None or not path.is_file():
                        raise OSError("missing")
                    path.resolve().relative_to(
                        (uploads_root() / str(self.user_id)).resolve()
                    )
                    data = path.read_bytes()
                except OSError, RuntimeError, TypeError, ValueError:
                    raise SemanticExportError(
                        "semantic-export-resource-unavailable"
                    ) from None
                if (
                    len(data) != resource.byte_length
                    or hashlib.sha256(data).hexdigest() != resource.sha256
                ):
                    raise SemanticExportError("semantic-export-resource-changed")
                return data
        raise SemanticExportError("semantic-export-resource-not-found")


def _materialized_html(
    request: SemanticExportRequest,
    resolved: dict[str, bytes],
    directory: Path,
) -> str:
    by_ref = {resource.ref: resource for resource in request.resources}
    soup = BeautifulSoup(request.semantic_html, "html.parser")
    for image in soup.find_all("img"):
        source = str(image.get("src") or "").strip()
        ref = parse_opaque_resource_path(source)
        if ref is None:
            continue
        resource = by_ref[ref]
        path = directory / resource.filename
        # Filename collisions would make a target ambiguous.  The contract is
        # intentionally strict rather than silently renaming source resources.
        if (
            path.exists()
            and hashlib.sha256(path.read_bytes()).hexdigest() != resource.sha256
        ):
            raise SemanticExportError("semantic-export-resource-filename-conflict")
        path.write_bytes(resolved[resource.ref])
        image["src"] = path.as_uri()
    return serialize_semantic_html(soup)


def _standalone_html(
    request: SemanticExportRequest,
    resolved: dict[str, bytes],
) -> bytes:
    """Inline exact owned raster resources into one downloadable HTML file."""

    supported = {"image/gif", "image/jpeg", "image/png", "image/webp"}
    by_ref = {resource.ref: resource for resource in request.resources}
    soup = BeautifulSoup(request.semantic_html, "html.parser")
    normalize_export_note_roles(soup)
    for image in soup.find_all("img"):
        ref = parse_opaque_resource_path(str(image.get("src") or "").strip())
        if ref is None:
            continue
        resource = by_ref[ref]
        if resource.media_type.lower() not in supported:
            raise SemanticExportError("semantic-export-html-resource-media")
        encoded = base64.b64encode(resolved[ref]).decode("ascii")
        image["src"] = f"data:{resource.media_type.lower()};base64,{encoded}"
    return project_diagrams_html(serialize_semantic_html(soup)).encode("utf-8")


def _docx_metadata_html(html: str) -> str:
    """Preserve repeated DC values in DOCX's scalar custom properties."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    if soup.head is not None:
        for node in soup.head.find_all("meta", attrs={"name": True}):
            name = str(node.get("name") or "")
            if not name.startswith("DC."):
                continue
            if name in fields:
                first = fields[name]
                first["content"] = "; ".join(
                    [str(first.get("content") or ""), str(node.get("content") or "")]
                )
                node.decompose()
            else:
                fields[name] = node
    return serialize_semantic_html(soup)


def export_semantic_document(
    request_value: SemanticExportRequest | dict,
    *,
    resolver: DocumentResourceResolver,
    run_managed_process: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ExportedSemanticArtifact:
    request = (
        request_value
        if isinstance(request_value, SemanticExportRequest)
        else SemanticExportRequest.from_dict(request_value)
    )
    _validate_target_resources(request)
    cover = normalize_epub_cover(request.metadata.get("epub-cover"))
    if cover and request.format != "epub":
        raise SemanticExportError("semantic-export-cover-format")
    # Every projection, including byte-exact HTML, proves that the declared
    # manifest still belongs to this provider scope before returning an artifact.
    resolved = {
        resource.ref: resolver.resolve(resource) for resource in request.resources
    }
    if request.format == "html":
        data = _standalone_html(request, resolved)
        return ExportedSemanticArtifact(
            SemanticExportArtifact(
                format="html",
                media_type="text/html; charset=utf-8",
                byte_length=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                validation_profile=VALIDATION_PROFILE,
                validation_profile_version=VALIDATION_PROFILE_VERSION,
            ),
            data,
        )
    if request.format in {"docx", "pdf"} and not callable(run_managed_process):
        raise SemanticExportError(f"semantic-export-{request.format}-unavailable")
    if request.format == "pdf":
        try:
            from .print_export import render_pdf_export

            data = render_pdf_export(
                request.semantic_html,
                metadata=request.metadata,
                images={
                    opaque_resource_path(ref): image for ref, image in resolved.items()
                },
                run_managed_process=run_managed_process,
                start_h2_on_new_page=request.start_h2_on_new_page,
            )
        except ImportError:
            raise SemanticExportError("semantic-export-pdf-unavailable") from None
        except OSError, RuntimeError, subprocess.SubprocessError, ValueError:
            raise SemanticExportError("semantic-export-pdf-conversion-failed") from None
        return ExportedSemanticArtifact(
            SemanticExportArtifact(
                format="pdf",
                media_type="application/pdf",
                byte_length=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                validation_profile=VALIDATION_PROFILE,
                validation_profile_version=VALIDATION_PROFILE_VERSION,
            ),
            data,
        )
    with tempfile.TemporaryDirectory(prefix="carlina-semantic-export-") as temporary:
        directory = Path(temporary)
        html = _materialized_html(request, resolved, directory)
        document = SimpleNamespace(
            output_html=html,
            output_markdown="",
            file_path=None,
            user_id=None,
            metadata_record=None,
            # The contract freezes nested containers; legacy packagers consume
            # ordinary JSON lists and dictionaries (including contributor rows).
            settings={"document_metadata": request.to_dict()["metadata"]},
        )
        output = directory / f"export.{request.format}"
        if request.format == "epub":
            from flask import current_app, has_app_context

            from .epub_packager import EPUBPackager
            from .epub_validation import validate_epub_file

            packager = EPUBPackager(
                document,
                str(output),
                include_source_toc=request.include_source_toc,
                image_mode=request.epub_image_mode,
                allowed_resource_roots=[directory],
                cover=cover,
                cover_image=(
                    resolver.resolve_pdf_cover(request, cast(int, cover["page"]))
                    if cover.get("mode") == "pdf_page"
                    else None
                ),
                options=request.epub_options or None,
            )
            packager.build()
            validation = validate_epub_file(
                str(output),
                config=current_app.config if has_app_context() else None,
            )
            if not validation.passed:
                raise SemanticExportError("semantic-export-epub-validation-failed")
            quality = packager.quality_report(
                structure="passed",
                epubcheck=validation.epubcheck,
                epubcheck_version=validation.version,
                ace=validation.ace,
                ace_version=validation.ace_version,
            )
            if quality is not None:
                # Resource materialization may rewrite opaque references in the
                # packager input. Keep the receipt bound to the immutable
                # provider request instead of those temporary paths.
                quality["input_sha256"] = request.semantic_html_sha256
            data = output.read_bytes()
        else:
            quality = None
            try:
                from .docx_notes import preserve_repeated_docx_notes
                from .docx_preprocessor import preprocess_html_for_docx
                from .print_export import docx_reference_path, finish_docx_export
            except ImportError:
                raise SemanticExportError("semantic-export-docx-unavailable") from None
            prepared = preprocess_html_for_docx(
                _docx_metadata_html(html), allowed_resource_roots=[directory]
            )
            try:
                executor = run_managed_process
                if not callable(executor):
                    raise SemanticExportError(
                        "semantic-export-docx-unavailable"
                    ) from None
                result = executor(
                    [
                        "pandoc",
                        "--from=html",
                        "--to=docx",
                        f"--reference-doc={docx_reference_path()}",
                        "--output",
                        str(output),
                        "--quiet",
                        *(
                            ["--metadata", f"lang={request.metadata['language']}"]
                            if request.metadata.get("language")
                            else []
                        ),
                        "--resource-path",
                        str(directory),
                    ],
                    runtime_unit="document_processing",
                    input_text=prepared,
                    timeout_seconds=120,
                    check=False,
                )
                if getattr(result, "returncode", None) != 0 or not output.is_file():
                    raise SemanticExportError(
                        "semantic-export-docx-conversion-failed"
                    ) from None
                data = preserve_repeated_docx_notes(output.read_bytes(), prepared)
                data = finish_docx_export(
                    data,
                    request.semantic_html,
                    title=(
                        request.metadata.get("title")
                        if isinstance(request.metadata.get("title"), str)
                        else None
                    ),
                    start_h2_on_new_page=request.start_h2_on_new_page,
                )
            except SemanticExportError:
                raise
            except OSError, RuntimeError, subprocess.SubprocessError:
                raise SemanticExportError(
                    "semantic-export-docx-conversion-failed"
                ) from None
    media_type = (
        "application/epub+zip"
        if request.format == "epub"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return ExportedSemanticArtifact(
        SemanticExportArtifact(
            format=request.format,
            media_type=media_type,
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            validation_profile=VALIDATION_PROFILE,
            validation_profile_version=VALIDATION_PROFILE_VERSION,
            quality=quality,
        ),
        data,
    )
