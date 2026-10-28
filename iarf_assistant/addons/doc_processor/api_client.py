import hashlib
import json
import logging
from pathlib import Path

from carlina.core.utc import utc_iso

from .cover_candidate import normalize_cover_candidate
from .metadata_service import (
    get_canonical_document_metadata,
    normalize_document_metadata_payload,
)
from .semantic_document import (
    canonicalize_document_html,
    is_current_semantic_document,
    semantic_result_for_stored_html,
)
from .semantic_export import capture_snapshot_resources

logger = logging.getLogger(__name__)


DOCUMENT_SNAPSHOT_CONTRACT_VERSION = "document-snapshot-v1"


# This cross-add-on DTO carries bounded source facts, including note arrangement.
# Extractor traces, page decisions, and arbitrary settings stay inside Doc Processor.
_PUBLIC_METADATA_TEXT_LIMITS = {
    "title": 512,
    "subtitle": 256,
    "publication_type": 64,
    "publisher": 255,
    "publication_date": 50,
    "journal_or_series": 255,
    "volume": 50,
    "issue": 50,
    "isbn": 50,
    "issn": 50,
}
_PUBLIC_METADATA_LIST_LIMIT = 4
_PUBLIC_METADATA_NAME_LIMIT = 128
_PUBLIC_METADATA_ROLE_LIMIT = 64
_PUBLIC_METADATA_IDENTIFIER_LIMIT = 8
_PUBLIC_METADATA_IDENTIFIER_VALUE_LIMIT = 128
_PUBLIC_METADATA_IDENTIFIER_LABEL_LIMIT = 64
_PUBLIC_METADATA_BYTES_LIMIT = 8 * 1024


def _canonical_output_for_read(document) -> tuple[str, object]:
    """Return stored bytes for current rows, canonical bytes for legacy rows.

    Legacy reads deliberately do not mutate the provider row.  The resulting
    object is still useful at the cross-add-on boundary, while completion and
    manual edits are the only places that establish persistent identity.
    """

    raw_html = str(getattr(document, "output_html", "") or "")
    settings = getattr(document, "settings", None)
    if is_current_semantic_document(settings):
        return raw_html, semantic_result_for_stored_html(raw_html, settings)
    result = canonicalize_document_html(
        raw_html,
        user_id=getattr(document, "user_id", 0),
        source_file_path=getattr(document, "file_path", None),
        relaxed=True,
    )
    return result.html, result


def _markdown_for_semantic_html(html_content: str) -> str:
    from .format_utils import html_to_markdown_on_the_fly

    return html_to_markdown_on_the_fly(html_content)


def _bounded_text(value, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:maximum] if value else None


def _bounded_contributors(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    bounded: dict[str, object] = {}
    for role in ("authors", "editors", "translators"):
        names = value.get(role)
        if not isinstance(names, list):
            continue
        clean_names = [
            name
            for raw_name in names[:_PUBLIC_METADATA_LIST_LIMIT]
            if (name := _bounded_text(raw_name, _PUBLIC_METADATA_NAME_LIMIT))
        ]
        if clean_names:
            bounded[role] = clean_names

    other = value.get("other")
    if isinstance(other, list):
        other_entries = []
        for raw_entry in other[:_PUBLIC_METADATA_LIST_LIMIT]:
            if not isinstance(raw_entry, dict):
                continue
            role = _bounded_text(raw_entry.get("role"), _PUBLIC_METADATA_ROLE_LIMIT)
            name = _bounded_text(raw_entry.get("name"), _PUBLIC_METADATA_NAME_LIMIT)
            if role and name:
                other_entries.append({"role": role, "name": name})
        if other_entries:
            bounded["other"] = other_entries
    return bounded or None


def _bounded_identifiers(value) -> list[dict] | None:
    if not isinstance(value, list):
        return None
    identifiers = []
    for raw_identifier in value[:_PUBLIC_METADATA_IDENTIFIER_LIMIT]:
        if not isinstance(raw_identifier, dict):
            continue
        identifier_value = _bounded_text(
            raw_identifier.get("value"), _PUBLIC_METADATA_IDENTIFIER_VALUE_LIMIT
        )
        if not identifier_value:
            continue
        entry = {"value": identifier_value}
        scheme = _bounded_text(raw_identifier.get("scheme"), 32)
        label = _bounded_text(
            raw_identifier.get("label"), _PUBLIC_METADATA_IDENTIFIER_LABEL_LIMIT
        )
        if scheme:
            entry["scheme"] = scheme
        if label:
            entry["label"] = label
        identifiers.append(entry)
    return identifiers or None


def _metadata_encoded_size(metadata: dict[str, object]) -> int:
    try:
        return len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    except OverflowError, TypeError, UnicodeError, ValueError:
        return _PUBLIC_METADATA_BYTES_LIMIT + 1


def _bounded_metadata(raw_metadata: object) -> dict:
    """Return the bounded, explicit metadata part of the source DTO."""

    normalized = normalize_document_metadata_payload(raw_metadata)
    metadata: dict[str, object] = {}
    for field, maximum in _PUBLIC_METADATA_TEXT_LIMITS.items():
        value = _bounded_text(normalized.get(field), maximum)
        if value:
            metadata[field] = value

    language = _bounded_text(normalized.get("language"), 50)
    if language:
        metadata["language"] = language
    contributors = _bounded_contributors(normalized.get("contributors"))
    if contributors:
        metadata["contributors"] = contributors
    identifiers = _bounded_identifiers(normalized.get("identifiers"))
    if identifiers:
        metadata["identifiers"] = identifiers
    note_arrangement = normalized.get("note_arrangement")
    if isinstance(note_arrangement, dict):
        metadata["note_arrangement"] = note_arrangement
    cover_candidate = normalize_cover_candidate(normalized.get("cover_candidate"))
    if cover_candidate:
        metadata["cover_candidate"] = cover_candidate

    # The individual caps above are intentionally conservative. Keep this
    # final byte guard so a future allow-list addition cannot silently turn a
    # source listing into an unbounded transport payload.
    while _metadata_encoded_size(metadata) > _PUBLIC_METADATA_BYTES_LIMIT:
        changed = False
        identifiers = metadata.get("identifiers")
        if isinstance(identifiers, list) and len(identifiers) > 1:
            identifiers.pop()
            changed = True
        contributors = metadata.get("contributors")
        if isinstance(contributors, dict):
            for role in ("other", "translators", "editors", "authors"):
                names = contributors.get(role)
                if isinstance(names, list) and len(names) > 1:
                    names.pop()
                    changed = True
                    break
        if changed:
            continue
        # All collection members are now at their minimum useful cardinality;
        # trim the least structural text fields before dropping any allow-listed
        # field entirely.
        for field in ("subtitle", "publisher", "journal_or_series"):
            value = metadata.get(field)
            if isinstance(value, str) and len(value) > 1:
                metadata[field] = value[: max(1, len(value) // 2)]
                changed = True
                break
        if not changed:
            break

    while _metadata_encoded_size(metadata) > _PUBLIC_METADATA_BYTES_LIMIT:
        arrangement = metadata.get("note_arrangement")
        pages = (
            arrangement.get("source_pages") if isinstance(arrangement, dict) else None
        )
        if isinstance(pages, list) and pages:
            pages.pop()
            continue

        evidence = (
            arrangement.get("evidence") if isinstance(arrangement, dict) else None
        )
        if isinstance(arrangement, dict) and isinstance(evidence, str) and evidence:
            arrangement["evidence"] = evidence[: len(evidence) // 2]
            continue

        for field in (
            "identifiers",
            "contributors",
            "subtitle",
            "publisher",
            "journal_or_series",
            "publication_type",
            "publication_date",
            "volume",
            "issue",
            "isbn",
            "issn",
            "language",
            "title",
        ):
            if field in metadata:
                metadata.pop(field)
                break
        else:
            if "cover_candidate" in metadata:
                metadata.pop("cover_candidate")
                continue
            break
    return metadata


class DocProcessorClient:
    """Document-processing hub backed by built-in and capability engines."""

    def __init__(self, context, config):
        self.context = context
        self.config = config or {}

    def _user_id(self) -> int:
        user_id = self.config.get("current_user_id")
        if user_id is None:
            raise ValueError(
                "current_user_id is required to access document resources."
            )
        return int(user_id)

    @staticmethod
    def _source_payload(document, *, include_content: bool = False) -> dict:
        """Return the stable, user-scoped document resource contract."""

        raw_metadata = get_canonical_document_metadata(document)
        metadata = _bounded_metadata(raw_metadata)
        settings = document.settings
        semantic_contract_mode = "relaxed_legacy"
        if (
            isinstance(settings, dict)
            and settings.get("semantic_contract_mode") == "completed"
            and is_current_semantic_document(settings)
        ):
            semantic_contract_mode = "completed"
        payload = {
            "id": document.id,
            "filename": document.filename,
            "status": document.status,
            # Consumers can reject incompatible rows from a lightweight list
            # without reading and hashing every full document snapshot.
            "semantic_contract_mode": semantic_contract_mode,
            "error_message": document.error_message,
            "created_at": (
                document.created_at.isoformat() if document.created_at else None
            ),
            "updated_at": (
                document.updated_at.isoformat() if document.updated_at else None
            ),
            "language": metadata.get("language"),
            "metadata": metadata,
        }
        if include_content:
            output_html, semantic = _canonical_output_for_read(document)
            payload.update(
                {
                    "output_html": output_html,
                    "output_markdown": (
                        document.output_markdown
                        if is_current_semantic_document(document.settings)
                        else _markdown_for_semantic_html(output_html)
                    ),
                    "semantic_contract_version": semantic.contract_version,
                    "semantic_content_hash": semantic.content_hash,
                }
            )
        return payload

    def get_documents(
        self,
        document_ids: list[str] | tuple[str, ...] | None = None,
        *,
        status: str | None = None,
        include_content: bool = False,
    ) -> list[dict]:
        """List document resources without exposing provider-owned ORM objects."""

        from sqlalchemy.orm import selectinload

        from .models import ProcessedDocument

        query = ProcessedDocument.query.filter_by(user_id=self._user_id()).options(
            selectinload(ProcessedDocument.metadata_record)
        )
        if document_ids is not None:
            normalized_ids = [str(item) for item in document_ids if str(item)]
            if not normalized_ids:
                return []
            query = query.filter(ProcessedDocument.id.in_(normalized_ids))
        if status:
            query = query.filter(ProcessedDocument.status == str(status))
        documents = query.order_by(ProcessedDocument.created_at.desc()).all()
        return [
            self._source_payload(document, include_content=include_content)
            for document in documents
        ]

    def get_document(
        self,
        document_id: str,
        *,
        include_content: bool = False,
    ) -> dict | None:
        """Return one owned document resource, or ``None`` without leaking access."""

        documents = self.get_documents(
            [str(document_id)],
            include_content=include_content,
        )
        return documents[0] if documents else None

    @staticmethod
    def _snapshot_provenance(settings: object) -> dict:
        """Expose only stable source identifiers from provider settings.

        Document settings may also contain paths, prompts, model choices, and
        other operational/private data.  The translator source contract must
        not accidentally turn those implementation details into a public
        cross-add-on API, so provenance is deliberately allow-listed.
        """

        if not isinstance(settings, dict):
            return {}

        provenance: dict[str, object] = {}
        allowed_keys = {
            "knowledge_source_id",
            "knowledge_source_version_id",
            "knowledge_source_version_number",
            "knowledge_source_content_hash",
        }
        for key in allowed_keys:
            value = settings.get(key)
            if (
                value not in (None, "")
                and isinstance(value, (str, int))
                and not isinstance(value, bool)
            ):
                provenance[key] = value
        return provenance

    def get_document_snapshot(self, document_id: str) -> dict:
        """Return one immutable-in-call, user-scoped completed HTML snapshot.

        The provider row and output are read once. ``content_hash`` is
        calculated from the exact UTF-8 bytes returned in ``output_html``;
        callers can therefore persist the DTO and safely use it after the
        provider document is edited. ``captured_at`` is the actual UTC time
        this DTO was assembled; ``source_updated_at`` separately preserves
        the provider row's update timestamp. Missing or unowned documents are
        intentionally reported as the same ``LookupError`` so this boundary
        never leaks whether another user owns a document. A found document
        that is not complete or has blank output raises ``ValueError``.
        """

        from .models import ProcessedDocument

        normalized_id = str(document_id or "").strip()
        if not normalized_id:
            raise LookupError("Document was not found.")

        # Do not call get_document(include_content=True) here: this operation
        # is the source-of-truth snapshot read and must hash the same value it
        # returns, without a second ORM query or a mutable ORM object crossing
        # the capability boundary.
        document = ProcessedDocument.query.filter_by(
            id=normalized_id,
            user_id=self._user_id(),
        ).first()
        if document is None:
            raise LookupError("Document was not found.")

        if str(document.status or "").lower() != "completed":
            raise ValueError("Document processing has not completed.")

        output_html, semantic = _canonical_output_for_read(document)
        if not isinstance(output_html, str) or not output_html.strip():
            raise ValueError("Completed document has no HTML output.")

        # Resource paths are provider implementation details.  The snapshot
        # carries only opaque, owner-attested references; the private mapping
        # remains in Document Processor and is checked again at export time.
        resources, output_html = capture_snapshot_resources(document)
        html_bytes = output_html.encode("utf-8")
        raw_metadata = get_canonical_document_metadata(document)
        settings = document.settings
        semantic_contract_mode = "relaxed_legacy"
        if (
            isinstance(settings, dict)
            and settings.get("semantic_contract_mode") == "completed"
            and is_current_semantic_document(settings)
        ):
            # ``_canonical_output_for_read`` has already called
            # ``semantic_result_for_stored_html`` for current rows.  Requiring
            # the mode explicitly here keeps the old read compatibility (the
            # semantic helper still accepts omitted modes) while ensuring the
            # cross-add-on snapshot never overstates an unattested row.
            semantic_contract_mode = "completed"
        captured_at = utc_iso(timespec="microseconds")
        snapshot = {
            "id": document.id,
            "document_id": document.id,
            "snapshot_contract_version": DOCUMENT_SNAPSHOT_CONTRACT_VERSION,
            "filename": document.filename,
            "status": document.status,
            "output_html": output_html,
            "content_hash": hashlib.sha256(html_bytes).hexdigest(),
            "semantic_contract_version": semantic.contract_version,
            "semantic_contract_mode": semantic_contract_mode,
            # The source handed to Translator is the opaque-resource semantic
            # target, so its identity is necessarily the transformed bytes.
            "semantic_content_hash": hashlib.sha256(html_bytes).hexdigest(),
            "captured_at": captured_at,
            "source_updated_at": (
                utc_iso(document.updated_at, timespec="microseconds")
                if document.updated_at
                else None
            ),
            "metadata": _bounded_metadata(raw_metadata),
            "provenance": self._snapshot_provenance(document.settings),
            "resources": resources,
        }
        if str(document.file_path or "").lower().endswith(".pdf"):
            from .cover_source import owned_pdf_source
            from .epub_cover import pdf_source_sha256

            try:
                snapshot["provenance"]["source_pdf_sha256"] = pdf_source_sha256(
                    owned_pdf_source(document, user_id=self._user_id())
                )
            except (OSError, ValueError):
                # Historical documents can outlive their uploads. Their
                # semantic target remains usable without a PDF cover.
                pass
        return snapshot

    def semantic_export_preflight(self, request: dict) -> dict:
        """Review an immutable EPUB projection without reading resource files."""

        from carlina.core.epub_export import (
            epub_json_value,
            semantic_epub_review_sha256,
        )
        from carlina.core.semantic_export import (
            SemanticExportContractError,
            SemanticExportRequest,
        )

        from .epub_quality import preflight_epub

        export_request = SemanticExportRequest.from_dict(request)
        if export_request.format != "epub" or not export_request.epub_options:
            raise SemanticExportContractError("epub-preflight-profile")
        # Resolve the actor just as for export; the target itself is the
        # caller's verified immutable DTO, with no foreign document lookup.
        self._user_id()
        result = preflight_epub(export_request.semantic_html, epub_json_value(export_request.metadata), export_request.epub_options)
        result["review_hash"] = semantic_epub_review_sha256(export_request.to_dict())
        return result

    def semantic_export(self, request: dict) -> dict:
        """Project an exact opaque-resource semantic target through this provider.

        The returned payload is deliberately a receipt plus bytes for the host
        caller; it never exposes Document Processor paths, rows, or settings.
        """

        from .semantic_export import DocumentResourceResolver, export_semantic_document

        artifact = export_semantic_document(
            request,
            resolver=DocumentResourceResolver(user_id=self._user_id()),
            run_managed_process=getattr(self.context, "run_managed_process", None),
        )
        return {"artifact": artifact.receipt.to_dict(), "data": artifact.data}

    def get_job_status(self, job_id: str) -> dict:
        """Return a public status payload for a document-processing job."""

        status = self.context.get_task_queue("doc_processor").get_status(job_id)
        if not isinstance(status, dict):
            return {"status": "unknown", "message": "Job not found."}
        public_status = dict(status)
        public_status.pop("traceback", None)
        public_status.pop("user_id", None)
        return public_status

    def _extractor_candidates(self) -> tuple:
        if not self.context:
            return ()
        try:
            return tuple(self.context.capability_candidates("extractors") or ())
        except LookupError, RuntimeError:
            logger.debug(
                "Document extractor capability catalog is unavailable", exc_info=True
            )
            return ()

    @staticmethod
    def _candidate_ids(candidate) -> set[str]:
        qualifiers = candidate.provider.qualifiers
        return {
            str(value).strip().lower()
            for value in (
                candidate.addon_id,
                candidate.provider.id,
                qualifiers.get("engine"),
            )
            if str(value or "").strip()
        }

    def _engine_from_candidate(self, candidate) -> dict:
        qualifiers = candidate.provider.qualifiers
        engine_id = str(qualifiers.get("engine") or candidate.provider.id).strip()
        addon = self.context.get_addon(candidate.addon_id)
        manifest = getattr(addon, "manifest", {}) or {}
        name = str(
            qualifiers.get("display_name")
            or manifest.get("name")
            or engine_id.replace("_", " ").title()
        )
        formats = [str(item).lower() for item in qualifiers.get("formats") or ()]
        features = [str(item).lower() for item in qualifiers.get("features") or ()]
        return {
            "id": engine_id,
            "name": name,
            "capabilities": list(dict.fromkeys([*formats, *features])),
            "formats": formats,
            "features": features,
            "addon_id": candidate.addon_id,
            "provider_capability_id": candidate.provider.id,
            "operations": list(candidate.provider.operations),
            "status": "active",
        }

    def _external_engines(self) -> list[dict]:
        return [
            self._engine_from_candidate(candidate)
            for candidate in self._extractor_candidates()
        ]

    def get_engines(self) -> list[dict]:
        """Return built-in parsers plus compatible active extractor engines."""

        engines = [
            {
                "id": "llm_vision",
                "name": "LLM Vision Parser (Advanced Semantic)",
                "capabilities": ["pdf", "images", "tables", "equations", "layout"],
                "formats": ["pdf", "png", "jpg", "jpeg", "webp", "bmp", "tiff", "tif"],
                "features": ["images", "tables", "equations", "layout"],
                "operations": ["extract"],
                "status": "active",
            },
            {
                "id": "native",
                "name": "Native Document Parser (Pandoc / MSG)",
                "capabilities": [
                    "docx",
                    "doc",
                    "epub",
                    "fb2",
                    "odt",
                    "html",
                    "htm",
                    "msg",
                    "txt",
                    "csv",
                    "rtf",
                ],
                "formats": [
                    "docx",
                    "doc",
                    "epub",
                    "fb2",
                    "odt",
                    "html",
                    "htm",
                    "msg",
                    "txt",
                    "csv",
                    "rtf",
                ],
                "features": [],
                "operations": ["extract"],
                "status": "active",
            },
        ]
        engines.extend(self._external_engines())
        return sorted(engines, key=lambda engine: engine["name"].lower())

    def _engine_supports_suffix(self, engine: dict, suffix: str) -> bool:
        if not suffix:
            return True

        formats = set(engine.get("formats") or engine.get("capabilities") or ())
        features = set(engine.get("features") or engine.get("capabilities") or ())
        image_extensions = {"png", "jpg", "jpeg", "webp", "bmp", "tiff", "tif"}

        if suffix in image_extensions and "images" in features:
            return True
        return suffix in formats or "any" in formats

    def _select_auto_engine(self, filepath: str, engines: list[dict], **kwargs) -> str:
        suffix = Path(filepath).suffix.lower().lstrip(".")

        if kwargs.get("force_ocr"):
            preferred_ids = ["pymupdf", "mineru", "marker", "llm_vision", "paddleocr"]
        elif kwargs.get("no_ocr"):
            preferred_ids = ["pymupdf"]
        elif suffix == "pdf":
            preferred_ids = ["pymupdf", "mineru", "marker", "llm_vision", "paddleocr"]
        else:
            preferred_ids = []

        for preferred_id in preferred_ids:
            match = next(
                (
                    engine
                    for engine in engines
                    if engine["id"] == preferred_id
                    and self._engine_supports_suffix(engine, suffix)
                ),
                None,
            )
            if match:
                return match["id"]

        generic_match = next(
            (
                engine
                for engine in engines
                if self._engine_supports_suffix(engine, suffix)
            ),
            None,
        )
        if generic_match:
            return generic_match["id"]

        if suffix:
            raise ValueError(f"No active extraction engine supports '.{suffix}' files.")
        raise ValueError("No compatible extraction engine is available.")

    def select_engine(self, filepath: str, engine: str = "auto", **kwargs) -> str:
        engines = self.get_engines()
        if not engines:
            raise ValueError("No engines are currently installed and active.")
        if engine == "auto":
            return self._select_auto_engine(filepath, engines, **kwargs)
        return engine

    def _extractor_handle(self, engine_id: str):
        normalized = str(engine_id).strip().lower()
        candidate = next(
            (
                item
                for item in self._extractor_candidates()
                if normalized in self._candidate_ids(item)
            ),
            None,
        )
        if candidate is None:
            return None
        user_id = self.config.get("current_user_id")
        if user_id is None:
            raise ValueError("current_user_id is required to use an extractor engine.")
        return self.context.get_capability_provider(
            "extractors",
            int(user_id),
            provider_addon_id=candidate.addon_id,
            provider_capability_id=candidate.provider.id,
        )

    def parse_document(
        self,
        filepath: str,
        engine: str = "auto",
        metrics_dict: dict | None = None,
        **kwargs,
    ) -> str:
        """Select and execute a document extractor through its capability handle."""

        engines = self.get_engines()
        if not engines:
            return "Error: No engines are currently installed and active."

        try:
            target_id = self.select_engine(filepath, engine=engine, **kwargs)
        except ValueError as exc:
            return f"Error: {exc}"

        if target_id in ("llm_vision", "native"):
            from iarf_assistant.llm_service import LLMService

            from .parsers import process_file_headlessly

            user_id = self.config.get("current_user_id")
            llm_service = LLMService(user_id=user_id) if user_id else None
            return process_file_headlessly(
                filepath,
                user_id,
                llm_service,
                metrics_dict=metrics_dict,
                **kwargs,
            )

        selected_engine = next(
            (item for item in engines if item["id"] == target_id),
            None,
        )
        if selected_engine is None:
            return f"Error: Requested extraction engine '{engine}' is not available."

        suffix = Path(filepath).suffix.lower().lstrip(".")
        if not self._engine_supports_suffix(selected_engine, suffix):
            return (
                f"Error: Extraction engine '{target_id}' does not support "
                f"'.{suffix}' files."
            )

        try:
            handle = self._extractor_handle(target_id)
            if handle is None:
                return (
                    f"Error: Requested extraction engine '{engine}' is not available."
                )
            client = handle.initialize_client(self.config)
            logger.info("DocProcessor delegating %r to engine %r", filepath, target_id)
            return client.extract_document(
                filepath,
                metrics_dict=metrics_dict,
                **kwargs,
            )
        except Exception:
            logger.exception("Engine %r failed to parse a document", target_id)
            return "Error: The extraction engine failed."
