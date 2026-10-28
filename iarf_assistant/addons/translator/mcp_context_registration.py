from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from flask import Flask
from pydantic import Field

from ...core.mcp_tool_runtime import McpToolRuntime

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .api_client import TranslatorClient


def register_context_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    client: Callable[[Flask, int], TranslatorClient],
) -> None:
    @server.tool(
        name="carlina_translator_configure_batches",
        description=(
            "Adjust future batch input tokens/max blocks and optional context_mode on an idle project without "
            "duplicating it or replanning sentences. Read batch_settings.version from "
            "translator_get, then pass expected_version. Active work must first be paused "
            "and reach a safe terminal boundary. Existing plans, targets and frozen attempts stay unchanged."
        ),
    )
    async def translator_configure_batches(
        project_id: str,
        input_tokens: int,
        max_blocks: int,
        expected_version: str,
        context_mode: Literal["self_contained", "retained"] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).configure_batches(
                    project_id,
                    input_tokens=input_tokens,
                    max_blocks=max_blocks,
                    expected_version=expected_version,
                    context_mode=context_mode,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_context_get",
        description=(
            "Read a bounded preceding source/translation excerpt after compaction or transfer. "
            "Use the retained packet's continuity_lookup (source_plan_id and before_block_id). "
            "Returns up to max_segments complete verified aligned groups (1-3, default 3), "
            "skipping navigation/headings and never cutting a group. Owner/source-plan fenced, "
            "read-only and safe during active work; fetch only when context is missing."
        ),
    )
    async def translator_context_get(
        project_id: str,
        before_block_id: str,
        max_segments: Annotated[int, Field(strict=True, ge=1, le=3)] = 3,
        source_plan_id: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).context_get(
                    project_id,
                    before_block_id=before_block_id,
                    max_segments=max_segments,
                    source_plan_id=source_plan_id,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_research_dossier_submit",
        description=(
            "Submit owner-scoped external research evidence for a verified "
            "document-block project; it remains unverified until separately handled."
        ),
    )
    async def translator_research_dossier_submit(
        project_id: str,
        client_submission_id: str,
        payload: dict[str, Any],
        provenance: dict[str, Any] | None,
        scope: dict[str, Any],
        dossier_id: str | None = None,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).research_dossier_submit(
                    project_id,
                    client_submission_id=client_submission_id,
                    payload=payload,
                    provenance=provenance,
                    scope=scope,
                    dossier_id=dossier_id,
                    expected_revision=expected_revision,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc
