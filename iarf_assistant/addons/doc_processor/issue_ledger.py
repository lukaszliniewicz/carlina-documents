"""Owner-scoped durable issue reporting for completed document extraction."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from flask import current_app
from sqlalchemy import func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError

from carlina.core.utc import utc_now_db

from ...extensions import db
from .extraction_issues import (
    ISSUE_KINDS,
    IssueReportValidationError,
    validate_issue_reports,
)
from .mcp_review import _revision
from .mcp_service import DocProcessorMcpService, DocProcessorMcpServiceError
from .models import DocProcessorIssue, ProcessedDocument

STATUSES = frozenset({"open", "confirmed", "resolved", "dismissed"})
MAX_HISTORY_ENTRIES = 100
MAX_RECEIPTS = 32
MAX_DOCUMENT_ISSUES = 10_000
MAX_PAGE_SIZE = 100
MAX_NOTE_CHARS = 2_000
MAX_QUERY_CHARS = 200
MAX_GROUP_MEMBERS = 50
MAX_GROUP_READINGS = 10
GROUP_BY_VALUES = frozenset({"excerpt"})
_REVISION_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _error(message: str, code: str) -> DocProcessorMcpServiceError:
    return DocProcessorMcpServiceError(message, code=code)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _revision_for(document: ProcessedDocument) -> str | None:
    if document.output_html is None:
        return None
    return _revision(str(document.output_html))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _validate_expected_revision(value: object) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise _error(
            "expected_revision must be the revision returned for this document.",
            "invalid_revision",
        )
    return value


def _validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
        raise _error(
            "Provide a stable idempotency key of 1-128 identifier characters.",
            "invalid_idempotency_key",
        )
    return value


def _validate_page_number(value: object, *, nullable: bool) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or value < 1:
        raise _error("page_number must be a positive integer.", "invalid_page_number")
    return value


def _normalize_report(value: object) -> dict[str, Any]:
    try:
        return validate_issue_reports([value])[0]
    except IssueReportValidationError as exc:
        raise _error("The issue report is invalid.", exc.code) from None


def _normalize_note(value: object) -> str:
    if not isinstance(value, str):
        raise _error("note must be text.", "invalid_note")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip() or len(normalized) > MAX_NOTE_CHARS or any(
        (ord(character) < 32 and character != "\n")
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in normalized
    ):
        raise _error("note contains invalid characters or exceeds its limit.", "invalid_note")
    return normalized


def _document_source_key(idempotency_key: str) -> str:
    return "manual:" + hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()


def _extraction_source_key(
    document: ProcessedDocument,
    source_revision: str,
    page_number: int,
    report_key: str,
) -> str:
    settings = document.settings if isinstance(document.settings, dict) else {}
    generation = settings.get("mcp_execution_id", source_revision)
    if (
        not isinstance(generation, str)
        or not generation
        or len(generation) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in generation)
    ):
        raise _error("Document extraction generation is invalid.", "invalid_generation")
    seed = json.dumps(
        {
            "generation": generation,
            "revision": source_revision,
            "page_number": page_number,
            "key": report_key,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "extraction:" + hashlib.sha256(seed).hexdigest()


def _folded(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _stored_report(issue: DocProcessorIssue) -> dict[str, Any] | None:
    payload = issue.payload
    if not isinstance(payload, dict):
        return None
    report = payload.get("report")
    return report if isinstance(report, dict) else None


def _report_search_text(report: dict[str, Any]) -> str:
    readings = report.get("suggested_readings")
    parts = [report.get("excerpt"), report.get("reason")]
    if isinstance(readings, list):
        parts.extend(readings)
    return _folded(" \n ".join(part for part in parts if isinstance(part, str)))


def _excerpt_groups(issues: list[DocProcessorIssue]) -> list[dict[str, Any]]:
    """Group issues that report the same printed reading, most frequent first."""

    grouped: dict[str, list[tuple[DocProcessorIssue, dict[str, Any]]]] = {}
    for issue in issues:
        report = _stored_report(issue)
        excerpt = report.get("excerpt") if report is not None else None
        if report is None or not isinstance(excerpt, str) or not excerpt.strip():
            continue
        key = " ".join(unicodedata.normalize("NFKC", excerpt).split())
        grouped.setdefault(key, []).append((issue, report))
    groups: list[dict[str, Any]] = []
    for excerpt, members in grouped.items():
        readings: list[str] = []
        for _, report in members:
            for reading in report.get("suggested_readings") or []:
                if isinstance(reading, str) and reading not in readings:
                    readings.append(reading)
        groups.append(
            {
                "excerpt": excerpt,
                "count": len(members),
                "kinds": sorted({str(report.get("kind")) for _, report in members}),
                "statuses": dict(
                    sorted(Counter(issue.status for issue, _ in members).items())
                ),
                "pages": sorted(
                    {
                        issue.page_number
                        for issue, _ in members
                        if issue.page_number is not None
                    }
                )[:MAX_GROUP_MEMBERS],
                "suggested_readings": readings[:MAX_GROUP_READINGS],
                "issue_ids": [issue.id for issue, _ in members][:MAX_GROUP_MEMBERS],
            }
        )
    groups.sort(key=lambda group: (-group["count"], group["excerpt"]))
    return groups


class IssueLedgerService:
    """Read and mutate issue records through an owner-scoped document service."""

    def __init__(self, documents: DocProcessorMcpService):
        self.documents = documents

    def _document(self, document_id: str) -> ProcessedDocument:
        return self.documents._document(document_id, fresh=True)

    def _issue(self, document: ProcessedDocument, issue_id: str) -> DocProcessorIssue:
        issue = DocProcessorIssue.query.filter_by(
            id=str(issue_id or "").strip(),
            document_id=document.id,
            owner_user_id=self.documents.owner_user_id,
        ).populate_existing().first()
        if issue is None:
            raise _error("Issue was not found.", "not_found")
        return issue

    @staticmethod
    def _payload(issue: DocProcessorIssue) -> dict[str, Any]:
        payload = issue.payload
        if not isinstance(payload, dict):
            raise _error("Issue record is invalid.", "invalid_issue_record")
        return payload

    def _detail(
        self,
        document: ProcessedDocument,
        issue: DocProcessorIssue,
        *,
        current_revision: str | None = None,
    ) -> dict[str, Any]:
        if current_revision is None:
            current_revision = _revision_for(document)
        payload = self._payload(issue)
        report = payload.get("report")
        history = payload.get("history")
        if not isinstance(report, dict) or not isinstance(history, list):
            raise _error("Issue record is invalid.", "invalid_issue_record")
        return {
            "id": issue.id,
            "document_id": issue.document_id,
            "page_number": issue.page_number,
            "source_revision": issue.source_revision,
            "current_revision": current_revision,
            "context_stale": issue.source_revision != current_revision,
            "status": issue.status,
            "version": issue.version,
            "origin": payload.get("origin"),
            "report": report,
            "history": history,
            "created_at": _iso(issue.created_at),
            "updated_at": _iso(issue.updated_at),
        }

    def list(
        self,
        document_id: str,
        status: str | None = None,
        kind: str | None = None,
        page_number: int | None = None,
        offset: int = 0,
        limit: int = 50,
        query: str | None = None,
        group_by: str | None = None,
        include_history: bool = True,
    ) -> dict[str, Any]:
        document = self._document(document_id)
        if type(include_history) is not bool:
            raise _error(
                "include_history must be a boolean.", "invalid_include_history"
            )
        if query is not None and (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_QUERY_CHARS
        ):
            raise _error(
                f"query must be 1-{MAX_QUERY_CHARS} characters.", "invalid_query"
            )
        if group_by is not None and group_by not in GROUP_BY_VALUES:
            raise _error("group_by must be excerpt.", "invalid_group_by")
        if status is not None and (
            not isinstance(status, str) or status not in STATUSES
        ):
            raise _error("status is invalid.", "invalid_status")
        if kind is not None and (
            not isinstance(kind, str) or kind not in ISSUE_KINDS
        ):
            raise _error("kind is invalid.", "invalid_kind")
        if page_number is not None:
            page_number = _validate_page_number(page_number, nullable=False)
        if type(offset) is not int or offset < 0:
            raise _error("offset must be a non-negative integer.", "invalid_pagination")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise _error("limit must be between 1 and 100.", "invalid_pagination")

        base = DocProcessorIssue.query.filter_by(
            document_id=document.id,
            owner_user_id=self.documents.owner_user_id,
        )
        counts = {
            value: int(
                base.filter_by(status=value).with_entities(func.count(DocProcessorIssue.id)).scalar()
                or 0
            )
            for value in sorted(STATUSES)
        }
        filtered = base
        if status is not None:
            filtered = filtered.filter_by(status=status)
        if page_number is not None:
            filtered = filtered.filter_by(page_number=page_number)
        if kind is not None or query is not None or group_by is not None:
            # Report data is encrypted, so these filters run after a bounded SQL scan.
            # The scan remains owner/document scoped and fails closed on pathological ledgers.
            issue_count = int(base.with_entities(func.count(DocProcessorIssue.id)).scalar() or 0)
            if issue_count > MAX_DOCUMENT_ISSUES:
                raise _error("Issue ledger exceeds its configured limit.", "issue_limit")
            needle = _folded(query) if query is not None else None
            matching = [
                issue
                for issue in filtered.order_by(
                    DocProcessorIssue.created_at, DocProcessorIssue.id
                ).all()
                if (report := _stored_report(issue)) is not None
                and (kind is None or report.get("kind") == kind)
                and (needle is None or needle in _report_search_text(report))
            ]
            if group_by is not None:
                groups = _excerpt_groups(matching)
                return {
                    "document_id": document.id,
                    "current_revision": _revision_for(document),
                    "group_by": group_by,
                    "groups": groups[offset : offset + limit],
                    "total_groups": len(groups),
                    "total": len(matching),
                    "counts": counts,
                }
            total = len(matching)
            selected = matching[offset : offset + limit]
        else:
            total = int(filtered.with_entities(func.count(DocProcessorIssue.id)).scalar() or 0)
            selected = (
                filtered.order_by(DocProcessorIssue.created_at, DocProcessorIssue.id)
                .offset(offset)
                .limit(limit)
                .all()
            )
        revision = _revision_for(document)
        items = [self._detail(document, issue, current_revision=revision) for issue in selected]
        if not include_history:
            # Summary items: the document and revision are stated once above,
            # and each issue's review history is one issue_get away.
            for item in items:
                item.pop("document_id", None)
                item.pop("current_revision", None)
                item["history_count"] = len(item.pop("history", []))
        return {
            "document_id": document.id,
            "current_revision": revision,
            "items": items,
            "total": total,
            "counts": counts,
        }

    def get(self, document_id: str, issue_id: str) -> dict[str, Any]:
        document = self._document(document_id)
        issue = self._issue(document, issue_id)
        return self._detail(document, issue)

    def report(
        self,
        document_id: str,
        *,
        expected_revision: str,
        idempotency_key: str,
        page_number: int | None,
        report: object,
    ) -> dict[str, Any]:
        expected_revision = _validate_expected_revision(expected_revision)
        idempotency_key = _validate_idempotency_key(idempotency_key)
        page_number = _validate_page_number(page_number, nullable=True)
        normalized_report = _normalize_report(report)
        request_hash = _digest(
            {
                "operation": "report",
                "expected_revision": expected_revision,
                "page_number": page_number,
                "report": normalized_report,
            }
        )
        document = self._document(document_id)
        source_key = _document_source_key(idempotency_key)
        existing = DocProcessorIssue.query.filter_by(
            document_id=document.id,
            owner_user_id=self.documents.owner_user_id,
            source_key=source_key,
        ).populate_existing().first()
        if existing is not None:
            payload = self._payload(existing)
            # Source key plus immutable request fields preserves report idempotency even
            # after its receipt has rolled out of the bounded receipt window.
            if (
                payload.get("origin") != "manual"
                or payload.get("report") != normalized_report
                or existing.source_revision != expected_revision
                or existing.page_number != page_number
            ):
                raise _error("That idempotency key belongs to a different report.", "idempotency_conflict")
            return self._detail(document, existing)

        revision = _revision_for(document)
        self._require_writable_document(document, expected_revision, revision)
        count = int(
            DocProcessorIssue.query.filter_by(
                document_id=document.id,
                owner_user_id=self.documents.owner_user_id,
            ).with_entities(func.count(DocProcessorIssue.id)).scalar()
            or 0
        )
        if count >= MAX_DOCUMENT_ISSUES:
            raise _error("Issue ledger is at its configured limit.", "issue_limit")
        self._fence_document(document)
        now = utc_now_db()
        issue = DocProcessorIssue(
            id=str(uuid.uuid4()),
            document_id=document.id,
            owner_user_id=self.documents.owner_user_id,
            page_number=page_number,
            source_key=source_key,
            source_revision=expected_revision,
            status="open",
            version=1,
            payload={
                "origin": "manual",
                "report": normalized_report,
                "history": [],
                "receipts": [{"key": idempotency_key, "request_hash": request_hash}],
            },
            created_at=now,
            updated_at=now,
        )
        db.session.add(issue)
        self._commit_mutation("report")
        db.session.refresh(issue)
        db.session.expire(document)
        document = self._document(document_id)
        return self._detail(document, issue)

    def update(
        self,
        document_id: str,
        issue_id: str,
        *,
        expected_revision: str,
        expected_version: int,
        idempotency_key: str,
        status: str,
        note: str,
    ) -> dict[str, Any]:
        expected_revision = _validate_expected_revision(expected_revision)
        idempotency_key = _validate_idempotency_key(idempotency_key)
        if type(expected_version) is not int or expected_version < 1:
            raise _error("expected_version must be a positive integer.", "invalid_version")
        if not isinstance(status, str) or status not in STATUSES:
            raise _error("status is invalid.", "invalid_status")
        normalized_note = _normalize_note(note)
        request_hash = _digest(
            {
                "operation": "update",
                "expected_revision": expected_revision,
                "expected_version": expected_version,
                "status": status,
                "note": normalized_note,
            }
        )
        document = self._document(document_id)
        issue = self._issue(document, issue_id)
        payload = self._payload(issue)
        receipts = payload.get("receipts", [])
        if not isinstance(receipts, list):
            raise _error("Issue record is invalid.", "invalid_issue_record")
        for receipt in receipts:
            if isinstance(receipt, dict) and receipt.get("key") == idempotency_key:
                if receipt.get("request_hash") != request_hash:
                    raise _error(
                        "That idempotency key belongs to a different update.",
                        "idempotency_conflict",
                    )
                return self._detail(document, issue)

        revision = _revision_for(document)
        self._require_writable_document(document, expected_revision, revision)
        if issue.version != expected_version:
            raise _error("Issue changed; fetch it again before updating.", "version_conflict")
        if status == issue.status:
            raise _error("The issue already has that status.", "status_unchanged")
        if len(payload.get("history", [])) >= MAX_HISTORY_ENTRIES:
            raise _error("Issue status history is full.", "history_limit")

        self._fence_document(document)
        now = utc_now_db()
        updated_payload = dict(payload)
        updated_payload["history"] = [
            *payload.get("history", []),
            {
                "status": status,
                "note": normalized_note,
                "timestamp": now.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z"),
            },
        ]
        updated_payload["receipts"] = [
            *receipts[-(MAX_RECEIPTS - 1) :],
            {"key": idempotency_key, "request_hash": request_hash},
        ]
        changed = db.session.execute(
            update(DocProcessorIssue)
            .where(
                DocProcessorIssue.id == issue.id,
                DocProcessorIssue.document_id == document.id,
                DocProcessorIssue.owner_user_id == self.documents.owner_user_id,
                DocProcessorIssue.version == expected_version,
            )
            .values(
                status=status,
                version=expected_version + 1,
                payload=updated_payload,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if not isinstance(changed, CursorResult) or changed.rowcount != 1:
            db.session.rollback()
            raise _error("Issue changed; fetch it again before updating.", "version_conflict")
        self._commit_mutation("update")
        db.session.expire(issue)
        db.session.expire(document)
        document = self._document(document_id)
        issue = self._issue(document, issue_id)
        return self._detail(document, issue)

    def import_extraction_issues(
        self,
        document: ProcessedDocument,
        pages: list[dict[str, Any]],
        source_revision: str | None = None,
    ) -> list[dict[str, Any]]:
        """Import immutable extraction reports without committing the caller's unit."""

        document_id = getattr(document, "id", None)
        if not isinstance(document_id, str) or not document_id:
            raise _error("A document record is required.", "invalid_document")
        if getattr(document, "user_id", None) != self.documents.owner_user_id:
            raise _error("Document was not found.", "not_found")
        fresh_document = self._document(document_id)
        current_revision = _revision_for(fresh_document)
        if source_revision is None:
            source_revision = current_revision
        if not isinstance(source_revision, str) or not _REVISION_RE.fullmatch(source_revision):
            raise _error("source_revision is invalid.", "invalid_revision")
        if not isinstance(pages, list):
            raise _error("pages must be a list.", "invalid_issue_reports")

        seen_pages: set[int] = set()
        normalized_pages: list[tuple[int, list[dict[str, Any]]]] = []
        for page in pages:
            if not isinstance(page, dict) or set(page) != {"page_number", "issues"}:
                raise _error("Each page must contain page_number and issues.", "invalid_issue_reports")
            page_number = _validate_page_number(page["page_number"], nullable=False)
            if page_number in seen_pages:
                raise _error("Page numbers must be unique in one import.", "duplicate_page")
            seen_pages.add(page_number)
            try:
                reports = validate_issue_reports(page["issues"])
            except IssueReportValidationError as exc:
                raise _error("Extraction issue reports are invalid.", exc.code) from None
            normalized_pages.append((page_number, reports))

        existing_count = int(
            DocProcessorIssue.query.filter_by(
                document_id=fresh_document.id,
                owner_user_id=self.documents.owner_user_id,
            ).with_entities(func.count(DocProcessorIssue.id)).scalar()
            or 0
        )
        if existing_count > MAX_DOCUMENT_ISSUES:
            raise _error("Issue ledger exceeds its configured limit.", "issue_limit")

        results: list[dict[str, Any]] = []
        additions = 0
        for page_number, reports in normalized_pages:
            for report in reports:
                source_key = _extraction_source_key(
                    fresh_document,
                    source_revision,
                    page_number,
                    report["key"],
                )
                existing = DocProcessorIssue.query.filter_by(
                    document_id=fresh_document.id,
                    owner_user_id=self.documents.owner_user_id,
                    source_key=source_key,
                ).populate_existing().first()
                if existing is not None:
                    payload = self._payload(existing)
                    if (
                        payload.get("origin") != "extraction"
                        or payload.get("report") != report
                        or existing.source_revision != source_revision
                        or existing.page_number != page_number
                    ):
                        raise _error(
                            "An extraction source key was reused for different evidence.",
                            "idempotency_conflict",
                        )
                    results.append(self._detail(fresh_document, existing))
                    continue
                additions += 1
                if existing_count + additions > MAX_DOCUMENT_ISSUES:
                    raise _error("Issue ledger is at its configured limit.", "issue_limit")
                now = utc_now_db()
                issue = DocProcessorIssue(
                    id=str(uuid.uuid4()),
                    document_id=fresh_document.id,
                    owner_user_id=self.documents.owner_user_id,
                    page_number=page_number,
                    source_key=source_key,
                    source_revision=source_revision,
                    status="open",
                    version=1,
                    payload={
                        "origin": "extraction",
                        "report": report,
                        "history": [],
                        "receipts": [],
                    },
                    created_at=now,
                    updated_at=now,
                )
                db.session.add(issue)
                results.append(self._detail(fresh_document, issue))
        db.session.flush()
        return results

    @staticmethod
    def _require_writable_document(
        document: ProcessedDocument,
        expected_revision: str,
        current_revision: str | None,
    ) -> None:
        if document.status != "completed" or current_revision is None:
            raise _error("Issue reporting requires a completed extraction.", "document_not_completed")
        if current_revision != expected_revision:
            raise _error(
                "The extraction changed; fetch its current revision before editing.",
                "revision_conflict",
            )

    def _fence_document(self, document: ProcessedDocument) -> None:
        observed_updated_at = document.updated_at
        now = utc_now_db()
        if observed_updated_at is not None and now <= observed_updated_at:
            now = observed_updated_at + timedelta(microseconds=1)
        try:
            changed = db.session.execute(
                update(ProcessedDocument)
                .where(
                    ProcessedDocument.id == document.id,
                    ProcessedDocument.user_id == self.documents.owner_user_id,
                    ProcessedDocument.status == "completed",
                    ProcessedDocument.updated_at == observed_updated_at,
                )
                .values(updated_at=now)
                .execution_options(synchronize_session=False)
            )
        except OperationalError:
            db.session.rollback()
            raise _error(
                "The extraction is busy or changed; fetch its current revision.",
                "revision_conflict",
            ) from None
        if not isinstance(changed, CursorResult) or changed.rowcount != 1:
            db.session.rollback()
            raise _error(
                "The extraction changed while saving; fetch its current revision.",
                "revision_conflict",
            )

    @staticmethod
    def _commit_mutation(operation: str) -> None:
        try:
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            raise _error(
                "The issue ledger is busy or changed; fetch it again before retrying.",
                "revision_conflict" if operation == "report" else "version_conflict",
            ) from None
        except IntegrityError:
            db.session.rollback()
            raise _error(
                "The issue ledger changed; fetch it again before retrying.",
                "idempotency_conflict" if operation == "report" else "version_conflict",
            ) from None
        except Exception:
            db.session.rollback()
            raise


def import_extraction_issues(
    document: ProcessedDocument,
    pages: list[dict[str, Any]],
    source_revision: str | None = None,
) -> list[dict[str, Any]]:
    """Import extraction feedback from an active Flask application context."""

    owner_user_id = getattr(document, "user_id", None)
    if type(owner_user_id) is not int or owner_user_id <= 0:
        raise _error("A document record is required.", "invalid_document")
    context = SimpleNamespace(application=current_app._get_current_object())
    documents = DocProcessorMcpService(context, owner_user_id)
    return IssueLedgerService(documents).import_extraction_issues(
        document,
        pages,
        source_revision=source_revision,
    )
