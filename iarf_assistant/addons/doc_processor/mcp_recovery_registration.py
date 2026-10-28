"""Document Processor MCP assembly recovery registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from flask import Flask
from pydantic import Field

from ...core.mcp_tool_runtime import McpToolRuntime

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .mcp_service import DocProcessorMcpService


AssemblyPageRepair = Annotated[
    dict[str, Any],
    Field(
        json_schema_extra={
            "additionalProperties": False,
            "properties": {
                "page_number": {"type": "integer", "minimum": 1},
                "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "html": {"type": "string", "maxLength": 262144},
            },
            "required": ["page_number", "expected_sha256", "html"],
        }
    ),
]


def register_recovery_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    service: Callable[[Flask, int], DocProcessorMcpService],
) -> None:
    @server.tool(
        name="carlina_document_recover",
        description=(
            "Inspect or recover a passive PDF list-assembly failure using retained page responses. "
            "inspect returns coverage and a snapshot revision; fetch reads up to four selected page HTML/hash pairs. "
            "preview checks topology-only repairs against expected_revision and returns a preview_token. "
            "apply requires that token, unchanged repairs, expected_revision and an idempotency_key. "
            "Each repair has page_number, expected_sha256 and html. Text, printed labels, notes and root ownership must be preserved. "
            "Incomplete evidence, image-bearing pages and evaluator-finalized or duplicate-unit batches are outside this bounded recovery path. "
            "The original failed execution and accepted responses remain immutable; successful recovery records a new completed execution."
        ),
    )
    async def document_recover(
        execution_id: str,
        operation: Literal["inspect", "fetch", "preview", "apply"] = "inspect",
        page_numbers: Annotated[list[int], Field(max_length=4)] | None = None,
        repairs: Annotated[list[AssemblyPageRepair], Field(max_length=16)]
        | None = None,
        expected_revision: str | None = None,
        preview_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            from .assembly_recovery import AssemblyRecoveryService

            return await runtime.run(
                lambda: AssemblyRecoveryService(
                    service(runtime.app, principal.owner_user_id)
                ).run(
                    execution_id,
                    operation=operation,
                    page_numbers=page_numbers,
                    repairs=repairs,
                    expected_revision=expected_revision,
                    preview_token=preview_token,
                    idempotency_key=idempotency_key,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc
