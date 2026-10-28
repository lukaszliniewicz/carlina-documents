"""Document Processor MCP issue-ledger registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from flask import Flask
from pydantic import Field

from ...core.mcp_tool_runtime import McpToolRuntime

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .issue_ledger import IssueLedgerService
    from .mcp_service import DocProcessorMcpService


def register_issue_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    service: Callable[[Flask, int], DocProcessorMcpService],
) -> None:
    def document_issues(owner_user_id: int) -> IssueLedgerService:
        from .issue_ledger import IssueLedgerService

        return IssueLedgerService(service(runtime.app, owner_user_id))

    @server.tool(
        name="carlina_document_issues_list",
        description="List extraction uncertainties and confirmed problems for focused review. Filter by status, kind or physical page; paginate to finish. context_stale means the document changed since the report. An empty ledger is not proof of error-free extraction.",
    )
    async def document_issues_list(
        document_id: str,
        status: Literal["open", "confirmed", "resolved", "dismissed"] | None = None,
        kind: Literal[
            "ambiguous_text",
            "name_check",
            "structure",
            "diagram",
            "source_damage",
            "other",
        ]
        | None = None,
        page_number: Annotated[int, Field(strict=True, ge=1)] | None = None,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_issues(principal.owner_user_id).list(
                    document_id,
                    status=status,
                    kind=kind,
                    page_number=page_number,
                    offset=offset,
                    limit=limit,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_issue_get",
        description="Read one extraction issue, its original report, corroborating references, review history, version and current document revision. External references corroborate but do not override the scan.",
    )
    async def document_issue_get(document_id: str, issue_id: str) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_issues(principal.owner_user_id).get(
                    document_id, issue_id
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_issue_report",
        description="Record a review issue without changing extraction text. report requires key, kind (ambiguous_text/name_check/structure/diagram/source_damage/other), excerpt, reason, suggested_readings (array), references (array of url/title/note). page_number is the physical PDF page or null. Requires current revision and an idempotency key.",
    )
    async def document_issue_report(
        document_id: str,
        expected_revision: str,
        idempotency_key: str,
        report: dict[str, Any],
        page_number: Annotated[int, Field(strict=True, ge=1)] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_issues(principal.owner_user_id).report(
                    document_id,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    report=report,
                    page_number=page_number,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_issue_update",
        description="Review an issue: open, confirmed, resolved or dismissed, with a required explanatory note. Requires current document revision and issue version. This changes only the ledger; correct document text separately, then resolve after checking the correction.",
    )
    async def document_issue_update(
        document_id: str,
        issue_id: str,
        expected_revision: str,
        expected_version: Annotated[int, Field(strict=True, ge=1)],
        idempotency_key: str,
        status: Literal["open", "confirmed", "resolved", "dismissed"],
        note: str,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_issues(principal.owner_user_id).update(
                    document_id,
                    issue_id,
                    expected_revision=expected_revision,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    status=status,
                    note=note,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc
