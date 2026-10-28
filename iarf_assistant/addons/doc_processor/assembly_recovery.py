"""Bounded, evidence-fenced assembly of completed passive PDF page batches."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm.attributes import flag_modified

from carlina.core.utc import utc_now_db

from carlina.content_storage import content_codec_from_context
from ...core.mcp_jobs import McpWorkError
from ...extensions import db
from ...models import McpExecution, McpWorkItem
from .batching import normalize_document_batch_plan
from .execution_recovery import _contract, _matches_source, settings_fingerprint
from .format_utils import html_to_markdown_on_the_fly
from .issue_ledger import import_extraction_issues
from .knowledge_adapter import sync_processed_document_source
from .mcp_review import _revision
from .mcp_service import DocProcessorMcpService, DocProcessorMcpServiceError
from .models import ProcessedDocument
from .note_arrangement import note_arrangement_enabled
from .page_batch_response import parse_page_batch_response_details
from .parsers.pdf_utils import concatenate_html_pages, normalize_extracted_page_html
from .passive_preflight import validate_page_declaration
from .semantic_document import canonicalize_document_html, semantic_settings

_ERRORS = frozenset(
    {"list-continuation-style-conflict", "list-continuation-ordinal-conflict"}
)
_MAX_PAGE = 256 * 1024
_MAX_REPAIR = 1024 * 1024
_MAX_FETCH = 1024 * 1024
_MAX_SNAPSHOT = 20 * 1024 * 1024
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class AssemblyRecoveryError(DocProcessorMcpServiceError):
    def __init__(self, message: str, *, code: str, context: dict | None = None):
        super().__init__(message, code=code)
        self.context = context or {}


def _error(code: str, message: str) -> AssemblyRecoveryError:
    return AssemblyRecoveryError(message, code=code)


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _stamp(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _raw_text_nodes(node: PageElement) -> tuple[str, ...]:
    """Keep source characters exactly, apart from structural formatting gaps."""
    if isinstance(node, NavigableString):
        parent = node.parent
        if (not str(node).strip() and isinstance(parent, Tag)
                and parent.name in {"ol", "ul", "main_content", "footnotes"}):
            return ()
        return (str(node),)
    if isinstance(node, Tag):
        return tuple(text for child in node.contents for text in _raw_text_nodes(child))
    return ()


def _attributes(tag: Tag, *, omit_continuation: bool = False) -> tuple:
    attrs = []
    for name, raw in sorted(tag.attrs.items()):
        if name == "class" and omit_continuation:
            raw = [item for item in (raw if isinstance(raw, list) else str(raw).split())
                   if item not in {"continues", "continues-next", "continued",
                                   "continued-from-previous"}]
            if not raw:
                continue
        value = tuple(raw) if isinstance(raw, list) else str(raw)
        attrs.append((name, value))
    return tuple(attrs)


def _ordinal(list_tag: Tag, items: list[Tag]) -> list[int | None]:
    if list_tag.name != "ol":
        return [None] * len(items)
    reversed_list = list_tag.has_attr("reversed")
    try:
        raw_start = list_tag.get("start")
        current = int(str(raw_start) if raw_start is not None else
                      (len(items) if reversed_list else 1))
    except (TypeError, ValueError):
        raise _error("invalid_repair", "Ordered list has an invalid start value.") from None
    result = []
    for item in items:
        if item.has_attr("value"):
            try:
                current = int(str(item["value"]))
            except (TypeError, ValueError):
                raise _error("invalid_repair", "List item has an invalid ordinal.") from None
        result.append(current)
        current += -1 if reversed_list else 1
    return result


def _protected_lineage(tag: Tag) -> tuple:
    """Keep section and non-list container ownership through list relocation."""
    ancestors = (
        (parent.name, _attributes(parent, omit_continuation=True))
        for parent in tag.parents
        if isinstance(parent, Tag)
        and parent.name not in {"[document]", "ol", "ul", "li"}
    )
    return tuple(reversed(tuple(ancestors)))


def _topology_signature(html: str) -> tuple:
    """Freeze visible and protected evidence while permitting list relocation.

    Each item is matched in source order with its effective style and ordinal.
    Wrapper splitting may duplicate an otherwise identical list declaration.
    """
    soup = BeautifulSoup(html, "html.parser")
    if any(not tag.find("li", recursive=False) for tag in soup.find_all(["ol", "ul"])):
        raise _error("invalid_repair", "Empty list wrappers cannot be recovered.")
    visible = _raw_text_nodes(soup)
    protected = []
    for tag in soup.find_all(True):
        if tag.name in {"ol", "ul", "li"}:
            continue
        direct = tuple(str(child) for child in tag.contents
                       if isinstance(child, NavigableString) and _raw_text_nodes(child))
        protected.append((tag.name, _attributes(tag, omit_continuation=True),
                          _protected_lineage(tag), direct))
    items = []
    for tag in soup.find_all("li"):
        parent = tag.parent
        if not isinstance(parent, Tag) or parent.name not in {"ol", "ul"}:
            raise _error("invalid_repair", "List items require an owning list.")
        siblings = parent.find_all("li", recursive=False)
        own_index = next(i for i, sibling in enumerate(siblings) if sibling is tag)
        own_ordinal = _ordinal(parent, siblings)[own_index]
        wrapper_attrs = tuple((name, value) for name, value in _attributes(parent, omit_continuation=True)
                              if name != "start")
        direct_label = tuple(
            text for child in tag.contents
            if not isinstance(child, Tag) or child.name not in {"ol", "ul"}
            for text in _raw_text_nodes(child)
        )
        items.append((tag.name, _attributes(tag, omit_continuation=True),
                      _protected_lineage(tag), parent.name, wrapper_attrs,
                      own_ordinal, direct_label))
    return visible, tuple(protected), tuple(items)


@dataclass(frozen=True)
class _Snapshot:
    execution: McpExecution
    document: ProcessedDocument
    pages: dict[int, str]
    blank: dict[int, bool]
    issues: list[dict[str, Any]]
    work: tuple[tuple[str, int, str], ...]
    revision: str
    batch_count: int
    boundary_flags: dict


class AssemblyRecoveryService:
    """Owner-scoped, no-provider recovery of a failed assembly stage."""

    def __init__(self, documents: DocProcessorMcpService):
        self.documents = documents

    def _snapshot(self, execution_id: str) -> _Snapshot:
        execution = db.session.get(McpExecution, str(execution_id), populate_existing=True)
        if execution is None or execution.owner_user_id != self.documents.owner_user_id:
            raise _error("not_found", "Execution was not found.")
        document = self.documents._document(execution.resource_id, fresh=True)
        if (execution.addon_id, execution.resource_kind, execution.mode) != (
            "doc_processor", "document", "passive"
        ) or execution.resource_id != document.id:
            raise _error("execution_conflict", "Execution is not a passive document run.")
        settings = document.settings if isinstance(document.settings, dict) else {}
        reason = (settings.get("mcp_recovery") or {}).get("reason")
        suspended_reason = (str(reason.get("error_code") or "").replace("_", "-")
                            if isinstance(reason, Mapping) else None)
        if not (
            (execution.status == "failed" and document.status == "error"
             and str(document.error_message or "") in _ERRORS)
            or (execution.status == "suspended" and document.status == "suspended"
                and suspended_reason in _ERRORS)
        ):
            raise _error("recovery_unavailable", "This execution has no eligible assembly conflict.")
        binding = settings.get("mcp_execution_id") or settings.get("mcp_last_execution_id")
        if binding != execution.id or settings.get("active_job_id"):
            raise _error("execution_conflict", "Document execution binding changed.")
        newer = db.session.query(McpExecution.id).filter(
            McpExecution.owner_user_id == execution.owner_user_id,
            McpExecution.addon_id == "doc_processor",
            McpExecution.resource_kind == "document",
            McpExecution.resource_id == document.id,
            McpExecution.created_at > execution.created_at,
            McpExecution.id != execution.id,
        ).first()
        if newer is not None:
            raise _error("execution_conflict", "A newer document execution exists.")
        contract = _contract(execution)
        if contract is None:
            raise _error("recovery_unavailable", "Frozen source proof is unavailable.")
        try:
            _matches_source(contract, self.documents._stored_source(document))
        except McpWorkError as exc:
            raise _error(exc.code, str(exc)) from None
        plan = execution.request_json.get("batch_plan") if isinstance(execution.request_json, dict) else None
        if not isinstance(plan, dict) or plan != settings.get("mcp_batch_plan"):
            raise _error("execution_conflict", "Frozen batch plan differs from document settings.")
        try:
            normalized = normalize_document_batch_plan(plan)
        except (TypeError, ValueError) as exc:
            raise _error("recovery_incomplete", "Batch plan is invalid.") from exc
        source = self.documents._stored_source(document)
        page_count = self.documents._pdf_page_count(source)
        if type(page_count) is not int or page_count < 1:
            raise _error("source_unavailable", "PDF page count is unavailable.")
        batches = normalized["batches"] if normalized else []
        covered = [number for batch in batches
                   for number in range(batch["start_page"], batch["end_page"] + 1)]
        if covered != list(range(1, page_count + 1)):
            raise _error("recovery_incomplete", "Batch plan does not cover every physical page.")
        rows = McpWorkItem.query.filter_by(execution_id=execution.id, dispatch_phase="page_extraction").all()
        by_batch: dict[str, list[McpWorkItem]] = {batch["batch_id"]: [] for batch in batches}
        for row in rows:
            if row.batch_id not in by_batch or row.state != "completed":
                raise _error("recovery_incomplete", "Page work is incomplete or outside the plan.")
            by_batch[row.batch_id].append(row)
        pages: dict[int, str] = {}
        blank: dict[int, bool] = {}
        issues: list[dict[str, Any]] = []
        work: list[tuple[str, int, str]] = []
        size = 0
        response_bytes = 0
        for batch in batches:
            entries = by_batch[batch["batch_id"]]
            completed_units = [row.unit_index for row in entries if row.state == "completed"]
            if len(completed_units) != len(set(completed_units)):
                raise _error("recovery_incomplete", "Duplicate page work unit exists.")
            terminal = [row for row in entries if row.state == "completed"
                        and row.is_batch_terminal_unit and type(row.unit_index) is int]
            if not terminal:
                raise _error("recovery_incomplete", "Accepted terminal page work is missing.")
            greatest = max(row.unit_index for row in terminal)
            candidates = [row for row in terminal if row.unit_index == greatest]
            if len(candidates) != 1:
                raise _error("recovery_incomplete", "Terminal page work is ambiguous.")
            selected = candidates[0]
            for row in entries:
                accepted = row.accepted_response_json
                if row.state == "completed" and not isinstance(accepted, dict):
                    raise _error("recovery_incomplete", "Accepted page response is missing.")
                if isinstance(accepted, dict):
                    encoded = _json(accepted)
                    response_bytes += len(encoded)
                    if response_bytes + size > _MAX_SNAPSHOT:
                        raise _error("recovery_too_large", "Recovery snapshot exceeds 20 MiB.")
                    work.append((row.id, row.version, _hash(encoded)))
            accepted = selected.accepted_response_json
            content = accepted.get("content") if isinstance(accepted, dict) else None
            if not isinstance(content, str):
                raise _error("recovery_incomplete", "Accepted page response is invalid.")
            numbers = list(range(batch["start_page"], batch["end_page"] + 1))
            try:
                parsed = parse_page_batch_response_details(
                    content, numbers,
                    allow_blank=settings.get("mcp_page_blank_policy") is not None,
                    allow_issues=settings.get("mcp_extraction_issues_policy") == "issues-v1",
                )
            except (TypeError, ValueError) as exc:
                raise _error("recovery_incomplete", "Accepted page response cannot be parsed.") from exc
            for number, page in parsed.items():
                declaration = validate_page_declaration(page.html, declared_blank=page.blank)
                if not declaration.valid:
                    raise _error("recovery_incomplete", "Accepted page declaration is invalid.")
                html = declaration.normalized_html or ""
                if BeautifulSoup(html, "html.parser").find(["img", "figure"]):
                    raise _error("recovery_incomplete", "Page image evidence requires normal review.")
                size += len(html.encode("utf-8"))
                if size + response_bytes > _MAX_SNAPSHOT:
                    raise _error("recovery_too_large", "Recovery snapshot exceeds 20 MiB.")
                pages[number] = html
                blank[number] = page.blank
                issues.append({"page_number": number, "issues": list(page.issues)})
        state = settings.get("pdf_state")
        parts = state.get("html_parts") if isinstance(state, dict) else None
        if not isinstance(parts, list) or len(parts) != page_count:
            raise _error("recovery_incomplete", "Page checkpoint does not match source count.")
        for number, checkpoint in enumerate(parts, 1):
            if checkpoint is None:
                continue
            if not isinstance(checkpoint, str) or normalize_extracted_page_html(checkpoint) != pages[number]:
                raise _error("recovery_incomplete", "Checkpoint differs from accepted page evidence.")
            pages[number] = checkpoint
        boundary_flags = state.get("boundary_flags") if isinstance(state, dict) else None
        if not isinstance(boundary_flags, dict):
            boundary_flags = {}
        evidence = {
            "execution": execution.id, "document": document.id,
            "execution_status": execution.status, "document_status": document.status,
            "execution_updated": _stamp(execution.updated_at),
            "document_updated": _stamp(document.updated_at),
            "source": contract["source"], "settings": settings_fingerprint(settings),
            "plan": normalized, "work": sorted(work),
            "pages": [(number, _hash(pages[number])) for number in sorted(pages)],
            "boundary_flags": _hash(_json(boundary_flags)),
        }
        return _Snapshot(execution, document, pages, blank, issues, tuple(sorted(work)),
                         _hash(_json(evidence)), len(batches), boundary_flags)

    def _candidate(self, snap: _Snapshot, repairs: list[dict]) -> tuple[str, list[int]]:
        changed = []
        pages = dict(snap.pages)
        for repair in repairs:
            number = repair["page_number"]
            original = pages[number]
            if _hash(original) != repair["expected_sha256"]:
                raise _error("revision_conflict", "A repaired page hash changed.")
            declaration = validate_page_declaration(repair["html"], declared_blank=snap.blank[number])
            if not declaration.valid:
                raise _error("invalid_repair", "Repair violates the page declaration.")
            proposed = declaration.normalized_html or ""
            if BeautifulSoup(proposed, "html.parser").find(["img", "figure"]):
                raise _error("invalid_repair", "Image evidence requires normal review.")
            if _topology_signature(original) != _topology_signature(proposed):
                raise _error("invalid_repair", "Repair changed text, attributes, labels or ordinals.")
            pages[number] = proposed
            if proposed != original:
                changed.append(number)
        try:
            merged = concatenate_html_pages(
                [pages[number] for number in sorted(pages)], snap.boundary_flags,
                toc_handling=(snap.document.settings.get("pdf_options") or {}).get("toc_handling", "omit"),
                preserve_note_labels=note_arrangement_enabled(snap.document.settings),
            )
        except ValueError as exc:
            reason = getattr(exc, "reason_code", None) or str(exc)
            if reason in _ERRORS:
                error = _error(reason, reason)
                context = {}
                for field in ("previous_page", "page_number", "section"):
                    value = getattr(exc, field, None)
                    if (field == "section" and value in {"main_content", "footnotes"}) or (
                        field != "section" and type(value) is int and 1 <= value <= len(snap.pages)
                    ):
                        context[field] = value
                error.context = context
                raise error from None
            raise _error("assembly_conflict", "Page assembly failed.") from exc
        semantic = canonicalize_document_html(
            merged, user_id=snap.document.user_id,
            source_file_path=snap.document.file_path,
        )
        if semantic.errors:
            raise _error("semantic_conflict", "Assembled document has unresolved semantic references.")
        return semantic.html, changed

    @staticmethod
    def _repairs(value: Any, pages: Mapping[int, str]) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 16:
            raise _error("invalid_repair", "At most 16 page repairs are allowed.")
        total = 0
        seen = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"page_number", "expected_sha256", "html"}:
                raise _error("invalid_repair", "A repair requires page_number, expected_sha256 and html.")
            number = item["page_number"]
            if type(number) is not int or number not in pages or number in seen:
                raise _error("invalid_repair", "Repair page is invalid or duplicated.")
            if not isinstance(item["expected_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["expected_sha256"]):
                raise _error("invalid_repair", "Expected page hash is invalid.")
            if not isinstance(item["html"], str):
                raise _error("invalid_repair", "Repair HTML must be text.")
            size = len(item["html"].encode("utf-8"))
            total += size
            if size > _MAX_PAGE or total > _MAX_REPAIR:
                raise _error("recovery_too_large", "Repair payload exceeds its byte limit.")
            seen.add(number)
        return sorted(value, key=lambda item: item["page_number"])

    def run(
        self, execution_id: str, operation: str = "inspect", *,
        page_numbers: list[int] | None = None, repairs: list[dict[str, Any]] | None = None,
        expected_revision: str | None = None, preview_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if operation not in {"inspect", "fetch", "preview", "apply"}:
            raise _error("invalid_operation", "Unsupported recovery operation.")
        if operation == "apply":
            if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
                raise _error("invalid_idempotency_key", "A bounded idempotency key is required.")
            request_hash = _hash(_json({"execution_id": execution_id, "repairs": repairs,
                                        "expected_revision": expected_revision,
                                        "preview_token": preview_token}))
            key_hash = _hash(f"{self.documents.owner_user_id}\0{execution_id}\0{idempotency_key}")
            execution = db.session.get(McpExecution, str(execution_id))
            if execution is not None and execution.owner_user_id == self.documents.owner_user_id:
                document = self.documents._document(execution.resource_id, fresh=True)
                receipt = (document.settings or {}).get("mcp_assembly_recovery_receipt")
                if isinstance(receipt, dict) and receipt.get("key_hash") == key_hash:
                    if receipt.get("request_hash") != request_hash:
                        raise _error("idempotency_conflict", "Idempotency key was reused for different input.")
                    return {**receipt, "idempotent": True}
        snap = self._snapshot(execution_id)
        base = {"execution_id": snap.execution.id, "document_id": snap.document.id,
                "snapshot_revision": snap.revision, "validated_pages": len(snap.pages),
                "validated_batches": snap.batch_count}
        if operation == "inspect":
            try:
                self._candidate(snap, [])
            except DocProcessorMcpServiceError as exc:
                return {**base, "ready": False, "conflict": exc.code,
                        "conflict_context": getattr(exc, "context", {})}
            return {**base, "ready": True, "conflict": None}
        if operation == "fetch":
            if not isinstance(page_numbers, list) or not 1 <= len(page_numbers) <= 4:
                raise _error("invalid_page_numbers", "Fetch requires one to four page numbers.")
            if any(type(number) is not int or number not in snap.pages for number in page_numbers) or len(set(page_numbers)) != len(page_numbers):
                raise _error("invalid_page_numbers", "Page numbers are invalid or duplicated.")
            selected = [{"page_number": number, "html": snap.pages[number],
                         "sha256": _hash(snap.pages[number]), "blank": snap.blank[number]}
                        for number in page_numbers]
            if sum(len(page["html"].encode("utf-8")) for page in selected) > _MAX_FETCH:
                raise _error("recovery_too_large", "Fetch exceeds 1 MiB.")
            return {**base, "pages": selected}
        if expected_revision != snap.revision:
            raise _error("revision_conflict", "Recovery snapshot changed; inspect again.")
        normalized = self._repairs(repairs, snap.pages)
        candidate, changed = self._candidate(snap, normalized)
        candidate_revision = _revision(candidate)
        token = _hash(_json({"snapshot": snap.revision, "repairs": normalized,
                             "candidate": _hash(candidate)}))
        result = {**base, "preview_token": token, "revision": candidate_revision,
                  "changed_pages": changed, "changed_page_count": len(changed)}
        if operation == "preview":
            return result
        if preview_token != token:
            raise _error("preview_conflict", "Preview token changed; preview again.")
        try:
            with db.session.no_autoflush:
                binding_key = ("mcp_execution_id" if
                               (snap.document.settings or {}).get("mcp_execution_id") == execution_id
                               else "mcp_last_execution_id")
                reserved_execution_result = db.session.execute(
                    update(McpExecution).where(
                        McpExecution.id == snap.execution.id,
                        McpExecution.owner_user_id == self.documents.owner_user_id,
                        McpExecution.status == snap.execution.status,
                        McpExecution.updated_at == snap.execution.updated_at,
                    ).values(updated_at=McpExecution.updated_at)
                    .execution_options(synchronize_session=False)
                )
                reserved_document_result = db.session.execute(
                    update(ProcessedDocument).where(
                        ProcessedDocument.id == snap.document.id,
                        ProcessedDocument.user_id == self.documents.owner_user_id,
                        ProcessedDocument.status == snap.document.status,
                        ProcessedDocument.updated_at == snap.document.updated_at,
                        ProcessedDocument.settings["active_job_id"].as_string().is_(None),
                        ProcessedDocument.settings[binding_key].as_string() == execution_id,
                    ).values(updated_at=ProcessedDocument.updated_at)
                    .execution_options(synchronize_session=False)
                )
                reserved_execution = (reserved_execution_result.rowcount
                                      if isinstance(reserved_execution_result, CursorResult) else 0)
                reserved_document = (reserved_document_result.rowcount
                                     if isinstance(reserved_document_result, CursorResult) else 0)
                if reserved_execution != 1 or reserved_document != 1:
                    raise _error("revision_conflict", "Recovery source changed before apply.")
                db.session.expire_all()
                locked = self._snapshot(execution_id)
                if locked.revision != snap.revision or locked.work != snap.work:
                    raise _error("revision_conflict", "Recovery evidence changed before apply.")
                document = locked.document
                new_execution_id = str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"carlina:assembly-recovery:{self.documents.owner_user_id}:{execution_id}:{key_hash}",
                ))
                now = utc_now_db()
                source_contract = _contract(locked.execution)
                if source_contract is None:
                    raise _error("recovery_unavailable", "Frozen source proof changed.")
                recovered_execution = McpExecution()
                recovered_execution.id = new_execution_id
                recovered_execution.owner_user_id = document.user_id
                recovered_execution.addon_id = "doc_processor"
                recovered_execution.resource_kind = "document"
                recovered_execution.resource_id = document.id
                recovered_execution.mode = "passive"
                recovered_execution.status = "completed"
                recovered_execution.created_at = now
                recovered_execution.updated_at = now
                recovered_execution.started_at = now
                recovered_execution.completed_at = now
                recovered_execution.request_json = {
                    "contract": "assembly-recovery-v1", "recovered_from": execution_id,
                    "batch_plan": locked.execution.request_json["batch_plan"],
                    "source_proof": source_contract["source"],
                }
                db.session.add(recovered_execution)
                audit = {"schema": "assembly-recovery-v1", "document_id": document.id,
                         "source_execution_id": execution_id, "new_execution_id": new_execution_id,
                         "snapshot_revision": snap.revision, "work": locked.work,
                         "repairs": [{"page_number": entry["page_number"],
                                      "original_sha256": _hash(snap.pages[entry["page_number"]]),
                                      "original_html": snap.pages[entry["page_number"]],
                                      "new_html": normalize_extracted_page_html(entry["html"]),
                                      "new_sha256": _hash(normalize_extracted_page_html(entry["html"]))}
                                     for entry in normalized]}
                encoded_audit = content_codec_from_context().encode_text(
                    _json(audit).decode("utf-8")
                )
                settings = semantic_settings(dict(document.settings or {}),
                    canonicalize_document_html(candidate, user_id=document.user_id,
                                               source_file_path=document.file_path), mode="completed")
                for field in ("pdf_state", "active_job_id", "mcp_execution_id", "mcp_execution_mode",
                              "mcp_recovery", "progress", "progress_msg", "extraction_issues_checkpoint"):
                    settings.pop(field, None)
                settings["mcp_last_execution_id"] = new_execution_id
                receipt = {"key_hash": key_hash, "request_hash": request_hash,
                           "source_execution_id": execution_id,
                           "new_execution_id": new_execution_id, "revision": candidate_revision,
                           "validated_pages": len(snap.pages), "validated_batches": snap.batch_count,
                           "changed_pages": changed}
                settings["mcp_assembly_recovery_receipt"] = receipt
                settings["mcp_assembly_recovery_audit"] = encoded_audit
                document.settings = settings
                flag_modified(document, "settings")
                document.status = "completed"
                document.output_html = candidate
                document.output_markdown = html_to_markdown_on_the_fly(candidate)
                document.error_message = None
                db.session.flush()
                import_extraction_issues(document, locked.issues, source_revision=candidate_revision)
                sync_processed_document_source(document)
            db.session.commit()
            return {**receipt, "idempotent": False}
        except Exception:
            db.session.rollback()
            raise
