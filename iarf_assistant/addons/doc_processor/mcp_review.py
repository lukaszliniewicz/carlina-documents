"""Bounded, owner-scoped review of completed extraction output.

Reading never re-extracts or rewrites a document. Edits use the same semantic
and knowledge publication contracts as the editor, with an optimistic fence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import OperationalError

from carlina.content_codec import ENCRYPTED_PREFIX, PLAINTEXT_PREFIX, ContentPolicyError
from carlina.content_storage import content_codec_from_context

from ...core.encryption import EncryptionError
from ...extensions import db
from .document_review import DocumentReview
from .format_utils import html_to_markdown_on_the_fly
from .knowledge_adapter import sync_processed_document_source
from .mcp_service import DocProcessorMcpService, DocProcessorMcpServiceError
from .mcp_settings import DocumentReviewEdit
from .models import ProcessedDocument
from .review_history import (
    MAX_HISTORY_BYTES,
    MAX_HISTORY_ENTRIES,
    inverse_splices,
    preview_splices,
    restore_splices,
)
from .semantic_document import (
    canonicalize_document_html,
    is_current_semantic_document,
    semantic_result_for_stored_html,
    semantic_settings,
)

MAX_REVIEW_HTML_BYTES = 20 * 1024 * 1024
MAX_REVIEW_EDIT_BYTES = 256 * 1024
RECEIPT_KEY = "mcp_review_receipts"
HISTORY_KEY = "mcp_review_history"
REVIEW_SCHEMA = "carlina.document-review.v1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _revision(html: str) -> str:
    # Bump REVIEW_SCHEMA when indexing semantics change. A cached node ID from
    # another index contract must not become a valid edit of a different node.
    return _digest(REVIEW_SCHEMA + "\0" + html)


class DocumentReviewError(DocProcessorMcpServiceError):
    def __init__(self, message: str, *, code: str, diagnostics=None):
        super().__init__(message, code=code)
        self.detail = message
        self.diagnostics = list(diagnostics or [])


def _error(message: str, code: str = "invalid_review_request", *, diagnostics=None):
    return DocumentReviewError(message, code=code, diagnostics=diagnostics)


class DocumentReviewService:
    def __init__(self, documents: DocProcessorMcpService):
        self.documents = documents

    def _snapshot(
        self,
        document_id: str,
        expected_revision: str | None = None,
        *,
        allow_invalid: bool = False,
    ):
        document = self.documents._document(document_id, fresh=True)
        if document.status != "completed" or not document.output_html:
            raise _error(
                "Review requires a completed extraction.", "document_not_completed"
            )
        raw = str(document.output_html)
        if len(raw.encode("utf-8")) > MAX_REVIEW_HTML_BYTES:
            raise _error("Document exceeds the review size limit.", "review_too_large")
        revision = _revision(raw)
        if expected_revision is not None and expected_revision != revision:
            raise _error(
                "The extraction changed; fetch fresh evidence before editing.",
                "revision_conflict",
            )
        if is_current_semantic_document(document.settings):
            semantic = semantic_result_for_stored_html(raw, document.settings)
        else:
            semantic = canonicalize_document_html(
                raw, user_id=document.user_id, source_file_path=document.file_path
            )
        if semantic.errors and not allow_invalid:
            raise _error(
                "Stored output has invalid semantic references.", "invalid_document"
            )
        return document, revision, DocumentReview(semantic.html)

    def read(
        self,
        document_id: str,
        operation: str,
        *,
        expected_revision: str | None = None,
        **options: Any,
    ):
        from .note_topology_repair import reference_candidates

        _document, revision, review = self._snapshot(
            document_id, expected_revision, allow_invalid=operation in {"notes", "note_references"}
        )
        readers = {
            "outline": review.outline,
            "inventory": review.inventory,
            "search": review.search,
            "fetch": review.fetch,
            "boundaries": review.boundaries,
            "notes": review.notes,
            "note_references": lambda **kwargs: reference_candidates(review, **kwargs),
        }
        if operation not in readers:
            raise _error("Unknown review operation.")
        try:
            evidence = readers[operation](**options)
        except ValueError as exc:
            raise _error(str(exc)) from None
        return {
            "document_id": document_id,
            "review_schema": REVIEW_SCHEMA,
            "revision": revision,
            **evidence,
        }

    def source_pages(
        self,
        document_id: str,
        page_numbers: list[int],
        *,
        max_edge: int = 1600,
        include_review_context: bool = False,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Render only requested full physical pages; never expose stored paths."""
        if type(include_review_context) is not bool:
            raise _error("include_review_context must be a boolean")
        review = None
        revision = None
        if include_review_context or expected_revision is not None:
            document, revision, review = self._snapshot(document_id, expected_revision)
        else:
            document = self.documents._document(document_id, fresh=True)
        if not isinstance(page_numbers, list) or not 1 <= len(page_numbers) <= 4:
            raise _error("Request one to four physical PDF page numbers.")
        if any(type(page) is not int or page < 1 for page in page_numbers) or len(
            set(page_numbers)
        ) != len(page_numbers):
            raise _error("Page numbers must be distinct positive integers.")
        if type(max_edge) is not int or not 400 <= max_edge <= 2400:
            raise _error("max_edge must be between 400 and 2400 pixels.")
        if not str(document.filename).lower().endswith(".pdf"):
            raise _error("Source-page images require a PDF.", "unsupported_source")
        source = self.documents._stored_source(document)
        try:
            import fitz

            images = []
            with fitz.open(source) as pdf:
                if any(page > len(pdf) for page in page_numbers):
                    raise _error("A requested physical page is outside the PDF.")
                for number in page_numbers:
                    page = pdf[number - 1]
                    edge = max(page.rect.width, page.rect.height)
                    if edge <= 0:
                        raise _error(
                            "Source page has invalid dimensions.", "source_unavailable"
                        )
                    pixmap = page.get_pixmap(
                        matrix=fitz.Matrix(max_edge / edge, max_edge / edge),
                        alpha=False,
                    )
                    encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                    images.append(
                        {
                            "page_number": number,
                            "width": pixmap.width,
                            "height": pixmap.height,
                            "image": {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{encoded}"
                                },
                            },
                        }
                    )
            payload = {
                "document_id": document_id,
                "page_numbering": "physical_one_based",
                "pages": images,
                "image_delivery": {
                    "transport": "native_mcp_image_content",
                    "image_count": len(images),
                    "model_visibility": "not_verified",
                    "guidance": "Forward native image blocks to the model; text serialization is not visual inspection.",
                },
            }
            if revision is not None:
                payload.update({"revision": revision, "review_schema": REVIEW_SCHEMA})
            if include_review_context and review is not None:
                by_page = {number: [] for number in page_numbers}
                offset = 0
                while True:
                    outline = review.outline(offset=offset, limit=200)
                    for index, heading in enumerate(outline["headings"], start=offset):
                        for number in page_numbers:
                            if number in heading["page_numbers"]:
                                by_page[number].append(
                                    {**heading, "outline_offset": index}
                                )
                    if outline["next_offset"] is None:
                        break
                    offset = outline["next_offset"]
                for image in images:
                    headings = by_page[image["page_number"]]
                    image["review_context"] = {
                        "headings": headings[:40],
                        "total_headings": len(headings),
                        "omitted_headings": max(0, len(headings) - 40),
                        "outline_next_offset": headings[40]["outline_offset"]
                        if len(headings) > 40
                        else None,
                    }
            return payload
        except DocProcessorMcpServiceError:
            raise
        except Exception:  # noqa: BLE001 - never expose source paths in renderer errors
            raise _error(
                "Source-page rendering is unavailable.", "source_unavailable"
            ) from None

    def apply(
        self,
        document_id: str,
        *,
        expected_revision: str,
        idempotency_key: str,
        edits: list[DocumentReviewEdit],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._validate_write_request(expected_revision, idempotency_key, dry_run)
        if (
            not isinstance(edits, list)
            or not 1 <= len(edits) <= 50
            or type(dry_run) is not bool
        ):
            raise _error("Provide 1–50 edits and a boolean dry_run.")
        operations = [
            edit.model_dump(exclude_none=True, exclude_defaults=True) for edit in edits
        ]
        request = json.dumps(
            {"revision": expected_revision, "edits": operations},
            sort_keys=True,
            ensure_ascii=False,
        )
        if len(request.encode("utf-8")) > MAX_REVIEW_EDIT_BYTES:
            raise _error("Edits exceed the 256 KiB request limit.", "review_too_large")
        request_hash = _digest(request)
        document = self.documents._document(document_id)
        receipts = list((document.settings or {}).get(RECEIPT_KEY) or [])
        duplicate = self._duplicate(document, receipts, idempotency_key, request_hash)
        if duplicate:
            return duplicate
        document, revision, review = self._snapshot(document_id, expected_revision)
        if not is_current_semantic_document(document.settings):
            raise _error(
                "Legacy output can be inspected, but must be normalized before scoped editing.",
                "invalid_document",
            )
        try:
            candidate = review.apply_edits(operations)
        except ValueError as exc:
            raise _error(
                str(exc), "invalid_edit", diagnostics=getattr(exc, "diagnostics", None)
            ) from None
        return self._persist_candidate(
            document, revision, candidate, operations,
            request_hash=request_hash, idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    def cleanup(
        self,
        document_id: str,
        *,
        roles: list[str] | None = None,
        page_numbers: list[int] | None = None,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Preview or atomically remove marked page headers/footers, with undo."""
        selected_roles = ["header", "footer"] if roles is None else roles
        if type(dry_run) is not bool:
            raise _error("dry_run must be a boolean.")
        if not dry_run:
            self._validate_write_request(expected_revision, idempotency_key, dry_run)
        request_hash = _digest(json.dumps({
            "operation": "remove_page_furniture", "revision": expected_revision,
            "roles": selected_roles, "page_numbers": page_numbers,
        }, sort_keys=True))
        document = self.documents._document(document_id)
        if not dry_run:
            duplicate = self._duplicate(
                document, list((document.settings or {}).get(RECEIPT_KEY) or []),
                idempotency_key, request_hash,
            )
            if duplicate:
                return duplicate
        document, revision, review = self._snapshot(document_id, expected_revision)
        if not is_current_semantic_document(document.settings):
            raise _error(
                "Normalize legacy output before removing marked page furniture.",
                "invalid_document",
            )
        try:
            candidate, summary = review.remove_page_furniture(selected_roles, page_numbers)
        except ValueError as exc:
            raise _error(str(exc), "invalid_edit") from None
        if not summary["removed_count"]:
            candidate = str(document.output_html)
        result = self._persist_candidate(
            document, revision, candidate,
            [{"element_id": "marked-page-furniture", "remove_page_furniture": True}],
            request_hash=request_hash, idempotency_key=idempotency_key or "cleanup-preview",
            dry_run=dry_run,
        )
        result["cleanup"] = summary
        return result

    def repair_boundaries(
        self, document_id: str, *, decisions: list[dict[str, str]] | None = None,
        page_numbers: list[int] | None = None, offset: int = 0, limit: int = 50,
        expected_revision: str | None = None, idempotency_key: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Review selected physical-page continuations through the shared undo path."""
        from .page_boundary_repair import repair_page_boundaries

        if type(dry_run) is not bool:
            raise _error("dry_run must be a boolean.")
        if not dry_run:
            self._validate_write_request(expected_revision, idempotency_key, dry_run)
            if not decisions:
                raise _error("Saving requires explicitly selected boundary decisions.")
        request_hash = _digest(json.dumps({
            "operation": "repair_page_boundaries", "revision": expected_revision,
            "decisions": decisions, "page_numbers": page_numbers,
        }, sort_keys=True))
        document = self.documents._document(document_id)
        if not dry_run:
            duplicate = self._duplicate(
                document, list((document.settings or {}).get(RECEIPT_KEY) or []),
                idempotency_key, request_hash,
            )
            if duplicate:
                return duplicate
        document, revision, review = self._snapshot(document_id, expected_revision)
        if not is_current_semantic_document(document.settings):
            raise _error("Normalize legacy output before repairing page boundaries.", "invalid_document")
        try:
            candidate, summary = repair_page_boundaries(
                review, decisions=decisions, page_numbers=page_numbers, offset=offset, limit=limit,
            )
        except ValueError as exc:
            raise _error(str(exc), "invalid_edit") from None
        result = self._persist_candidate(
            document, revision, candidate if candidate is not None else str(document.output_html),
            [{"element_id": "physical-page-boundaries", "repair_page_boundaries": True}],
            request_hash=request_hash, idempotency_key=idempotency_key or "boundary-preview",
            dry_run=dry_run,
        )
        result["boundary_repair"] = summary
        return result

    def repair_note_reference(
        self, document_id: str, *, element_id: str, note_element_id: str,
        after_text: str, label: str, expected_revision: str,
        idempotency_key: str, dry_run: bool = True,
    ) -> dict[str, Any]:
        """Restore one explicitly witnessed marker through the normal undo path."""
        from .note_reference_repair import repair_note_reference

        self._validate_write_request(expected_revision, idempotency_key, dry_run)
        request_hash = _digest(json.dumps({
            "operation": "repair_note_reference", "revision": expected_revision,
            "element_id": element_id, "note_element_id": note_element_id,
            "after_text": after_text, "label": label,
        }, sort_keys=True, ensure_ascii=False))
        document = self.documents._document(document_id)
        if not dry_run:
            duplicate = self._duplicate(
                document, list((document.settings or {}).get(RECEIPT_KEY) or []),
                idempotency_key, request_hash,
            )
            if duplicate:
                return duplicate
        document, revision, review = self._snapshot(document_id, expected_revision)
        if not is_current_semantic_document(document.settings):
            raise _error("Normalize legacy output before repairing note references.", "invalid_document")
        try:
            candidate, summary = repair_note_reference(
                review, element_id=element_id, note_element_id=note_element_id,
                after_text=after_text, label=label,
            )
        except ValueError as exc:
            raise _error(str(exc), "invalid_edit") from None
        result = self._persist_candidate(
            document, revision, candidate,
            [{"element_id": element_id, "repair_note_reference": True}],
            request_hash=request_hash, idempotency_key=idempotency_key, dry_run=dry_run,
        )
        result["note_reference_repair"] = summary
        return result

    def repair_note_topology(
        self, document_id: str, *, operations: list[dict[str, Any]], expected_revision: str,
        idempotency_key: str, dry_run: bool = True,
    ) -> dict[str, Any]:
        """Preview or apply witnessed note classification and destination repairs."""
        from .note_topology_repair import repair_note_topology

        self._validate_write_request(expected_revision, idempotency_key, dry_run)
        if len(json.dumps(operations, ensure_ascii=False).encode()) > MAX_REVIEW_EDIT_BYTES:
            raise _error("Note repairs exceed the edit size limit.", "review_too_large")
        request_hash = _digest(json.dumps({
            "operation": "repair_note_topology", "revision": expected_revision,
            "operations": operations,
        }, sort_keys=True, ensure_ascii=False))
        document = self.documents._document(document_id)
        if not dry_run:
            duplicate = self._duplicate(
                document, list((document.settings or {}).get(RECEIPT_KEY) or []),
                idempotency_key, request_hash,
            )
            if duplicate:
                return duplicate
        document, revision, review = self._snapshot(document_id, expected_revision)
        if not is_current_semantic_document(document.settings):
            raise _error("Normalize legacy output before repairing note relationships.", "invalid_document")
        try:
            candidate, summary = repair_note_topology(review, operations)
        except ValueError as exc:
            raise _error(str(exc), "invalid_edit") from None
        result = self._persist_candidate(
            document, revision, candidate,
            [{"element_id": item["element_id"], "repair_note_topology": True} for item in operations],
            request_hash=request_hash, idempotency_key=idempotency_key, dry_run=dry_run,
        )
        result["note_topology_repairs"] = summary
        return result

    def _persist_candidate(
        self, document, revision, candidate, operations, *,
        request_hash, idempotency_key, dry_run,
    ):
        """One atomic semantic/publication/history path for scoped corrections."""
        document_id = document.id
        receipts = list((document.settings or {}).get(RECEIPT_KEY) or [])
        observed_updated_at = document.updated_at
        semantic = self._validate_candidate(document, candidate)
        try:
            splices = inverse_splices(str(document.output_html), semantic.html)
        except ValueError as exc:
            raise _error(str(exc), "review_too_large") from None
        result = {
            "document_id": document_id,
            "previous_revision": revision,
            "revision": _revision(semantic.html),
            "semantic_content_hash": semantic.content_hash,
            "edit_count": len(operations),
            "changed": _revision(semantic.html) != revision,
            "dry_run": dry_run,
            "duplicate": False,
            "diagnostics": list(semantic.diagnostics),
            "preview": preview_splices(semantic.html, splices),
            "edit_results": [
                {
                    "element_id": operation["element_id"],
                    "operation": next(
                        name
                        for name in (
                            "heading_level",
                            "demote_heading",
                            "replacement_html",
                            "replace_text",
                            "replace_texts",
                            "delete_element",
                            "remove_page_furniture",
                            "repair_page_boundaries",
                            "repair_note_reference",
                            "repair_note_topology",
                        )
                        if name in operation
                    ),
                    **(
                        {
                            "matched_count": sum(
                                item["expected_matches"]
                                for item in operation["replace_texts"]
                            ),
                            "replacement_results": [
                                {
                                    "replacement_index": index,
                                    "matched_count": item["expected_matches"],
                                    "whole_word": item.get("whole_word", False),
                                }
                                for index, item in enumerate(operation["replace_texts"])
                            ],
                        }
                        if "replace_texts" in operation
                        else {}
                    ),
                    **(
                        {"matched_count": operation["replace_text"]["expected_matches"]}
                        if "replace_text" in operation
                        else {}
                    ),
                }
                for operation in operations
            ],
        }
        self._impact(result, document)
        history = self._history(document)
        if dry_run:
            result["proposed_revision"] = result.pop("revision")
            result["revision"] = revision
            result["undo_available_on_save"] = bool(splices)
            return result
        change_id = uuid.uuid4().hex if splices else None
        if splices:
            history.append(
                {
                    "document_id": document.id,
                    "owner_user_id": document.user_id,
                    "change_id": change_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "edit_count": len(operations),
                    "before_revision": revision,
                    "after_revision": _revision(semantic.html),
                    "undone": False,
                    "splices": splices,
                }
            )
        settings = semantic_settings(document.settings, semantic)
        if splices:
            settings["metadata_stale"] = True
        settings[HISTORY_KEY] = self._encode_history(history)
        settings[RECEIPT_KEY] = [
            *receipts[-31:],
            {
                "key": idempotency_key,
                "request_hash": request_hash,
                "before_revision": revision,
                "after_revision": _revision(semantic.html),
                "change_id": change_id,
            },
        ]
        self._save(
            document, semantic, settings, observed_updated_at, publish=bool(splices)
        )
        result["change_id"] = change_id
        result["undo_available"] = bool(splices)
        return result

    @staticmethod
    def _validate_write_request(expected_revision, idempotency_key, dry_run):
        if not isinstance(expected_revision, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_revision
        ):
            raise _error("expected_revision must be the revision returned by review.")
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", idempotency_key
        ):
            raise _error(
                "Provide a stable idempotency key of 1-128 identifier characters."
            )
        if type(dry_run) is not bool:
            raise _error("dry_run must be a boolean.")

    @staticmethod
    def _duplicate(document, receipts, idempotency_key, request_hash):
        for receipt in receipts:
            if isinstance(receipt, dict) and receipt.get("key") == idempotency_key:
                if receipt.get("request_hash") != request_hash:
                    raise _error(
                        "That idempotency key belongs to a different edit.",
                        "idempotency_conflict",
                    )
                return {
                    "document_id": document.id,
                    "duplicate": True,
                    "applied_revision": receipt["after_revision"],
                    "revision": _revision(str(document.output_html or "")),
                    "change_id": receipt.get("change_id"),
                }
        return None

    @staticmethod
    def _impact(result, document):
        result["metadata_stale"] = bool(
            result["changed"] or (document.settings or {}).get("metadata_stale")
        )
        result["exports_need_regeneration"] = result["changed"]
        result["existing_exports_changed"] = False
        result["existing_translator_snapshots_changed"] = False

    @staticmethod
    def _validate_candidate(document, candidate):
        if len(candidate.encode("utf-8")) > MAX_REVIEW_HTML_BYTES:
            raise _error(
                "Edited output exceeds the review size limit.", "review_too_large"
            )
        semantic = canonicalize_document_html(
            candidate, user_id=document.user_id, source_file_path=document.file_path
        )
        rejected_codes = sorted({
            item["code"] for item in semantic.diagnostics
            if item.get("level") == "error" or item.get("code") in {
                "active_or_embedded_tag_removed",
                "unsafe_attribute_removed",
                "unsafe_meta_removed",
                "unsupported_tag_unwrapped",
                "unsafe_url_removed",
                "invalid_attribute_removed",
            }
        })
        if rejected_codes:
            raise _error(
                "Edits contain unsupported markup or invalid semantic references "
                f"({', '.join(rejected_codes[:8])}).",
                "invalid_edit",
            )
        if not html_to_markdown_on_the_fly(semantic.html).strip():
            raise _error(
                "Edits must retain canonical text for knowledge publication.",
                "invalid_edit",
            )
        return semantic

    @staticmethod
    def _history(document) -> list[dict[str, Any]]:
        encoded = (document.settings or {}).get(HISTORY_KEY, [])
        try:
            if (
                not isinstance(encoded, list)
                or len(encoded) > MAX_HISTORY_ENTRIES
                or any(
                    not isinstance(item, str)
                    or not item.startswith((ENCRYPTED_PREFIX, PLAINTEXT_PREFIX))
                    for item in encoded
                )
                or sum(len(item) for item in encoded) > MAX_HISTORY_BYTES
            ):
                raise ValueError("invalid history")
            # Review history has always used versioned envelopes. The codec's
            # legacy row compatibility must not admit an untagged replacement.
            codec = content_codec_from_context()
            entries = [json.loads(codec.decode_text(item)) for item in encoded]
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or entry.get("document_id") != document.id
                    or entry.get("owner_user_id") != document.user_id
                    or not isinstance(entry.get("splices"), list)
                    or not isinstance(entry.get("change_id"), str)
                    or type(entry.get("undone")) is not bool
                ):
                    raise ValueError("invalid history binding")
            return entries
        except ContentPolicyError, EncryptionError, ValueError, TypeError, KeyError:
            raise _error(
                "Review history is unavailable or invalid.",
                "history_unavailable",
            ) from None

    @staticmethod
    def _encode_history(entries):
        codec = content_codec_from_context()
        encoded = [
            codec.encode_text(json.dumps(entry, ensure_ascii=False))
            for entry in entries[-MAX_HISTORY_ENTRIES:]
        ]
        while sum(len(item) for item in encoded) > MAX_HISTORY_BYTES:
            encoded.pop(0)
        return encoded

    def history(self, document_id: str, *, expected_revision: str | None = None):
        document, revision, _review = self._snapshot(document_id, expected_revision)
        entries = self._history(document)
        active = next(
            (entry for entry in reversed(entries) if not entry["undone"]), None
        )
        return {
            "document_id": document_id,
            "revision": revision,
            "review_schema": REVIEW_SCHEMA,
            "retention": {
                "max_entries": MAX_HISTORY_ENTRIES,
                "max_encrypted_bytes": MAX_HISTORY_BYTES,
            },
            "items": [
                {
                    **{
                        key: entry[key]
                        for key in (
                            "change_id",
                            "created_at",
                            "edit_count",
                            "before_revision",
                            "after_revision",
                            "undone",
                        )
                    },
                    "can_undo": entry is active and entry["after_revision"] == revision,
                }
                for entry in reversed(entries)
            ],
        }

    def undo(
        self,
        document_id: str,
        *,
        change_id: str,
        expected_revision: str,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._validate_write_request(expected_revision, idempotency_key, dry_run)
        if not isinstance(change_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", change_id
        ):
            raise _error("change_id must come from review history.")
        request_hash = _digest(
            json.dumps(
                {
                    "operation": "undo",
                    "revision": expected_revision,
                    "change_id": change_id,
                },
                sort_keys=True,
            )
        )
        document = self.documents._document(document_id)
        receipts = list((document.settings or {}).get(RECEIPT_KEY) or [])
        duplicate = self._duplicate(document, receipts, idempotency_key, request_hash)
        if duplicate:
            return duplicate
        document, revision, _review = self._snapshot(document_id, expected_revision)
        observed_updated_at = document.updated_at
        entries = self._history(document)
        active = next(
            (entry for entry in reversed(entries) if not entry["undone"]), None
        )
        if (
            active is None
            or active["change_id"] != change_id
            or active["after_revision"] != revision
        ):
            raise _error(
                "Only the latest unchanged retained correction can be undone.",
                "revision_conflict",
            )
        try:
            candidate = restore_splices(str(document.output_html), active["splices"])
        except ValueError, KeyError, TypeError:
            raise _error("Undo fragments are invalid.", "history_unavailable") from None
        if _revision(candidate) != active["before_revision"]:
            raise _error("Undo revision verification failed.", "history_unavailable")
        semantic = self._validate_candidate(document, candidate)
        if _revision(semantic.html) != active["before_revision"]:
            raise _error(
                "Previous output needs normalization and cannot be restored exactly.",
                "invalid_document",
            )
        # Reuse the forward fragments with their sides swapped. No second diff
        # (or larger history budget) is needed to preview an inverse operation.
        forward_preview = preview_splices(str(document.output_html), active["splices"])
        for fragment in forward_preview["fragments"]:
            fragment["before"], fragment["after"] = (
                fragment["after"],
                fragment["before"],
            )
            fragment["before_chars"], fragment["after_chars"] = (
                fragment["after_chars"],
                fragment["before_chars"],
            )
        result = {
            "document_id": document_id,
            "change_id": change_id,
            "previous_revision": revision,
            "revision": active["before_revision"],
            "semantic_content_hash": semantic.content_hash,
            "changed": True,
            "dry_run": dry_run,
            "duplicate": False,
            "preview": forward_preview,
        }
        self._impact(result, document)
        if dry_run:
            result["proposed_revision"] = result.pop("revision")
            result["revision"] = revision
            return result
        active["undone"] = True
        settings = semantic_settings(document.settings, semantic)
        settings["metadata_stale"] = True
        settings[HISTORY_KEY] = self._encode_history(entries)
        settings[RECEIPT_KEY] = [
            *receipts[-31:],
            {
                "key": idempotency_key,
                "request_hash": request_hash,
                "before_revision": revision,
                "after_revision": active["before_revision"],
                "change_id": change_id,
            },
        ]
        self._save(document, semantic, settings, observed_updated_at, publish=True)
        return result

    def _save(self, document, semantic, settings, observed_updated_at, *, publish):
        try:
            changed = db.session.execute(
                update(ProcessedDocument)
                .where(
                    ProcessedDocument.id == document.id,
                    ProcessedDocument.user_id == self.documents.owner_user_id,
                    ProcessedDocument.status == "completed",
                    ProcessedDocument.updated_at == observed_updated_at,
                )
                .values(
                    output_html=semantic.html,
                    output_markdown=html_to_markdown_on_the_fly(semantic.html),
                    settings=settings,
                    error_message=None,
                )
                .execution_options(synchronize_session=False)
            )
            if not isinstance(changed, CursorResult) or changed.rowcount != 1:
                raise _error(
                    "The extraction changed while saving; fetch fresh evidence.",
                    "revision_conflict",
                )
            db.session.expire(document)
            if publish:
                sync_processed_document_source(document)
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            raise _error(
                "The document is busy or changed; read it again before retrying.",
                "revision_conflict",
            ) from None
        except Exception:
            db.session.rollback()
            raise
