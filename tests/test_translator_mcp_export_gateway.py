from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask

from carlina.runtime_composition import resolve_runtime_composition
from iarf_assistant.core import mcp_gateway


class _ToolServer:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, description):
        def register(function):
            self.tools[name] = function
            return function

        return register


@pytest.fixture
def gateway_tools(monkeypatch):
    application = Flask(__name__)
    client = SimpleNamespace(
        export_document=MagicMock(return_value={"operation_id": "export-1"}),
        export_document_get=MagicMock(return_value={"ready": False}),
    )
    principal = MagicMock(return_value=SimpleNamespace(owner_user_id=41))
    factory = MagicMock(return_value=client)

    async def invoke_in_app(_application, function):
        return function()

    monkeypatch.setattr(mcp_gateway, "_principal_from_request", principal)
    monkeypatch.setattr(mcp_gateway, "_translator_client", factory)
    monkeypatch.setattr(mcp_gateway, "_in_app", invoke_in_app)
    server = _ToolServer()
    mcp_gateway._register_product_tools(
        server,
        application,
        composition=resolve_runtime_composition("workbench", environment={}),
    )
    return server.tools, application, client, principal, factory


def test_export_dispatches_with_principal_owner_and_retry_key(gateway_tools):
    tools, app, client, principal, factory = gateway_tools
    result = asyncio.run(
        tools["carlina_translator_export"]("project-1", "retry-key", "docx")
    )
    assert result == {"operation_id": "export-1"}
    principal.assert_called_once_with(required_scope="mcp.translator")
    factory.assert_called_once_with(app, 41)
    client.export_document.assert_called_once_with(
        "project-1", export_format="docx", idempotency_key="retry-key"
    )


def test_pdf_export_forwards_heading_flag(gateway_tools):
    tools, _app, client, _principal, _factory = gateway_tools
    asyncio.run(tools["carlina_translator_export"](
        "project-1", "pdf-key", "pdf", True,
    ))
    client.export_document.assert_called_once_with(
        "project-1", export_format="pdf", idempotency_key="pdf-key",
        start_h2_on_new_page=True,
    )


def test_export_get_dispatches_exact_operation_and_chunk_options(gateway_tools):
    tools, app, client, principal, factory = gateway_tools
    result = asyncio.run(
        tools["carlina_translator_export_get"](
            "project-1", "export-1", True, 123, 4096, "a" * 64
        )
    )
    assert result == {"ready": False}
    principal.assert_called_once_with(required_scope="mcp.translator")
    factory.assert_called_once_with(app, 41)
    client.export_document_get.assert_called_once_with(
        "project-1",
        "export-1",
        include_content=True,
        offset=123,
        limit=4096,
        expected_sha256="a" * 64,
    )


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("carlina_translator_export", ("project-1", "retry-key")),
        ("carlina_translator_export_get", ("project-1", "export-1")),
    ],
)
def test_export_denied_scope_never_constructs_client(gateway_tools, tool_name, args):
    tools, _app, _client, principal, factory = gateway_tools
    principal.side_effect = PermissionError("scope denied")
    with pytest.raises(PermissionError, match="scope denied"):
        asyncio.run(tools[tool_name](*args))
    factory.assert_not_called()


@pytest.mark.parametrize(
    "tool_name,method,args",
    [
        ("carlina_translator_export", "export_document", ("project-1", "retry-key")),
        (
            "carlina_translator_export_get",
            "export_document_get",
            ("project-1", "export-1"),
        ),
    ],
)
def test_export_unexpected_errors_do_not_expose_paths(
    gateway_tools, tool_name, method, args
):
    from mcp.server.mcpserver.exceptions import ToolError

    tools, _app, client, _principal, _factory = gateway_tools
    getattr(client, method).side_effect = RuntimeError("/private/user/export.epub")
    with pytest.raises(ToolError) as caught:
        asyncio.run(tools[tool_name](*args))
    assert "/private" not in str(caught.value)
    assert "operation_failed" in str(caught.value)
