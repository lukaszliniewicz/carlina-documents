"""Translator-owned MCP registration boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from flask import Flask

from ...core.mcp_tool_runtime import McpToolRuntime
from .mcp_context_registration import register_context_tools
from .mcp_glossary_registration import register_glossary_tools
from .mcp_project_registration import register_project_tools
from .mcp_registration_types import SourcePageRenderer, TranslatorExecutionStarter
from .mcp_review_registration import register_review_tools

if TYPE_CHECKING:
    from mcp.server import MCPServer
    from mcp_types import CallToolResult

    from ...core.executor_profile import ExecutorProfile
    from ...models import McpExecution
    from .api_client import TranslatorClient


def register_translator_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    client: Callable[[Flask, int], TranslatorClient],
    start_execution: TranslatorExecutionStarter,
    normalize_executor_profile: Callable[
        [str, Mapping[str, Any] | None], ExecutorProfile | None
    ],
    execution_payload: Callable[[McpExecution], dict[str, Any]],
    source_pages: SourcePageRenderer,
    image_result: Callable[[dict[str, Any]], CallToolResult],
) -> None:
    """Register Translator tools in their established catalog order."""

    register_project_tools(
        server,
        runtime,
        client=client,
        start_execution=start_execution,
        normalize_executor_profile=normalize_executor_profile,
        execution_payload=execution_payload,
    )
    register_review_tools(
        server,
        runtime,
        client=client,
        source_pages=source_pages,
        image_result=image_result,
    )
    register_glossary_tools(server, runtime, client=client)
    register_context_tools(server, runtime, client=client)
