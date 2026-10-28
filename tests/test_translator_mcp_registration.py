from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

from iarf_assistant.addons.translator.mcp_registration import (
    register_translator_tools,
)
from iarf_assistant.core.executor_profile import (
    ExecutorProfile,
    require_executor_profile,
)
from iarf_assistant.core.mcp_jobs import McpPrincipal
from iarf_assistant.core.mcp_tool_runtime import McpToolRuntime

_EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text"],
    "supports_tools": True,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}


class RecordingClient:
    def __init__(self, results: Mapping[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.results = dict(results or {})

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            result = self.results.get(name, {"ok": True})
            if isinstance(result, BaseException):
                raise result
            return result

        return record


def _principal(
    *,
    owner_user_id: int = 713,
    scopes: frozenset[str] | None = None,
    allowed_roots: tuple[str, ...] = (),
) -> McpPrincipal:
    return McpPrincipal(
        key_id="synthetic-translator-key",
        owner_user_id=owner_user_id,
        scopes=scopes or frozenset({"mcp.translator"}),
        allowed_roots=allowed_roots,
    )


def _register(
    tmp_path: Path,
    *,
    translator_client: RecordingClient | None = None,
    client_factory: Callable[[Flask, int], Any] | None = None,
    owner_user_id: int = 713,
    allowed_roots: tuple[str, ...] = (),
    authorize: Callable[[str], McpPrincipal] | None = None,
    failure: Callable[[BaseException], Exception] | None = None,
    start_execution: Callable[..., dict[str, Any]] | None = None,
    normalize_executor_profile: Callable[
        [str, Mapping[str, Any] | None], ExecutorProfile | None
    ]
    | None = None,
    source_pages: Callable[..., dict[str, Any]] | None = None,
    image_result: Callable[[dict[str, Any]], CallToolResult] | None = None,
) -> tuple[MCPServer, Flask, list[int], RecordingClient]:
    app = Flask("translator-mcp-registration", instance_path=str(tmp_path))
    principal = _principal(
        owner_user_id=owner_user_id,
        allowed_roots=allowed_roots,
    )
    client = translator_client or RecordingClient()
    client_owners: list[int] = []

    def get_client(_app: Flask, actual_owner_user_id: int) -> Any:
        client_owners.append(actual_owner_user_id)
        if client_factory is not None:
            return client_factory(_app, actual_owner_user_id)
        return client

    async def in_app(application: Flask, function: Callable[[], Any]) -> Any:
        with application.app_context():
            return function()

    def normalize(mode: str, value: Mapping[str, Any] | None) -> ExecutorProfile | None:
        if normalize_executor_profile is not None:
            return normalize_executor_profile(mode, value)
        if mode == "managed":
            return None
        return require_executor_profile(value)

    def start(
        application: Flask,
        *,
        owner_user_id: int,
        project_id: str,
        mode: str,
        executor_profile: ExecutorProfile | None = None,
        research_dossier_revision_id: str | None = None,
        context_mode: str | None = None,
    ) -> dict[str, Any]:
        if start_execution is not None:
            return start_execution(
                application,
                owner_user_id=owner_user_id,
                project_id=project_id,
                mode=mode,
                executor_profile=executor_profile,
                research_dossier_revision_id=research_dossier_revision_id,
                context_mode=context_mode,
            )
        return {"project_id": project_id, "mode": mode}

    def render_source_pages(
        actual_owner_user_id: int,
        document_id: str,
        page_numbers: list[int],
        *,
        max_edge: int,
    ) -> dict[str, Any]:
        if source_pages is not None:
            return source_pages(
                actual_owner_user_id,
                document_id,
                page_numbers,
                max_edge=max_edge,
            )
        return {
            "document_id": document_id,
            "page_numbers": page_numbers,
            "max_edge": max_edge,
        }

    def image(payload: dict[str, Any]) -> CallToolResult:
        if image_result is not None:
            return image_result(payload)
        return CallToolResult(
            content=[TextContent(type="text", text="rendered")],
            structuredContent=payload,
        )

    runtime = McpToolRuntime(
        app=app,
        authorize=authorize or (lambda _scope: principal),
        in_app=in_app,
        failure=failure
        or (lambda exc: ToolError(f"normalized {type(exc).__name__}: {exc}")),
    )
    server = MCPServer(name="translator-registration-test")
    register_translator_tools(
        server,
        runtime,
        client=get_client,
        start_execution=start,
        normalize_executor_profile=normalize,
        execution_payload=lambda execution: {
            "id": getattr(execution, "id", "execution")
        },
        source_pages=render_source_pages,
        image_result=image,
    )
    return server, app, client_owners, client


_EXPECTED_TOOL_NAMES = [
    "carlina_translator_create",
    "carlina_translator_start",
    "carlina_translator_get",
    "carlina_translator_epub_edition_get",
    "carlina_translator_epub_edition_save",
    "carlina_translator_epub_edition_clear",
    "carlina_translator_epub_preflight",
    "carlina_translator_export",
    "carlina_translator_export_get",
    "carlina_translator_delete_project",
    "carlina_translator_cover_import",
    "carlina_translator_export_metadata_update",
    "carlina_translator_list",
    "carlina_translator_numbering_preview",
    "carlina_translator_numbering_apply",
    "carlina_translator_numbering_undo",
    "carlina_translator_review_outline",
    "carlina_translator_review_fetch",
    "carlina_translator_review_source_pages",
    "carlina_translator_qa_diagnostics",
    "carlina_translator_review_coverage_record",
    "carlina_translator_review_coverage_get",
    "carlina_translator_review_search",
    "carlina_translator_review_explain",
    "carlina_translator_export_metadata_prepare",
    "carlina_translator_target_update",
    "carlina_translator_reader_note_create",
    "carlina_translator_reader_note_revise",
    "carlina_translator_reader_note_set_state",
    "carlina_translator_glossary_get",
    "carlina_translator_glossary_propose",
    "carlina_translator_shared_glossary_list",
    "carlina_translator_shared_glossary_get",
    "carlina_translator_shared_glossary_create",
    "carlina_translator_shared_glossary_project",
    "carlina_translator_shared_glossary_preview",
    "carlina_translator_shared_glossary_apply",
    "carlina_translator_shared_glossary_detach",
    "carlina_translator_shared_glossary_contribute",
    "carlina_translator_shared_glossary_review",
    "carlina_translator_configure_batches",
    "carlina_translator_context_get",
    "carlina_translator_research_dossier_submit",
]


def test_registration_publishes_the_ordered_40_tool_sdk_catalog_and_schemas(
    tmp_path: Path,
) -> None:
    server, _app, _owners, _client = _register(tmp_path)

    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == _EXPECTED_TOOL_NAMES
    catalog = {tool.name: tool for tool in tools}

    start_schema = catalog["carlina_translator_start"].input_schema
    assert start_schema["properties"]["mode"]["default"] == "passive"
    profile_schema = start_schema["properties"]["executor_profile"]["anyOf"][0]
    assert profile_schema == {"additionalProperties": True, "type": "object"}
    assert set(start_schema["required"]) == {"project_id"}

    preflight_schema = catalog["carlina_translator_epub_preflight"].input_schema
    assert set(preflight_schema["required"]) == {"project_id", "epub_options"}
    assert "export_metadata" not in preflight_schema["properties"]
    assert "epub_cover" in preflight_schema["properties"]
    assert preflight_schema["properties"]["numbering_revision"]["default"] is None

    export_schema = catalog["carlina_translator_export"].input_schema
    assert set(export_schema["required"]) == {"project_id", "idempotency_key"}
    assert export_schema["properties"]["format"]["enum"] == [
        "epub",
        "docx",
        "html",
        "pdf",
    ]
    assert export_schema["properties"]["start_h2_on_new_page"]["default"] is False
    assert "epub_options" in export_schema["properties"]
    assert "epub_review_hash" in export_schema["properties"]
    cover_mode = export_schema["properties"]["epub_cover_mode"]
    assert cover_mode["enum"] == ["existing", "pdf_page", "generated"]
    assert cover_mode["default"] == "existing"
    cover_page = export_schema["properties"]["epub_cover_page"]
    assert cover_page["default"] is None
    assert {"type": "integer", "minimum": 1, "maximum": 1_000_000} in cover_page[
        "anyOf"
    ]

    export_get = catalog["carlina_translator_export_get"].input_schema
    assert export_get["properties"]["limit"]["default"] == 65536
    assert export_get["properties"]["include_content"]["default"] is False

    apply_schema = catalog["carlina_translator_shared_glossary_apply"].input_schema
    assert apply_schema["properties"]["decisions"]["maxItems"] == 2000
    decision_ref = apply_schema["properties"]["decisions"]["items"]["$ref"].rsplit(
        "/", 1
    )[-1]
    assert apply_schema["$defs"][decision_ref]["additionalProperties"] is False


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("carlina_translator_get", {"project_id": "project-a"}),
        ("carlina_translator_review_outline", {"project_id": "project-a"}),
        ("carlina_translator_shared_glossary_list", {}),
        (
            "carlina_translator_context_get",
            {"project_id": "project-a", "before_block_id": "block-a"},
        ),
    ],
)
def test_scope_denial_precedes_client_creation_for_every_registration_group(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    requested_scopes: list[str] = []
    client_creations: list[int] = []

    def deny(scope: str) -> McpPrincipal:
        requested_scopes.append(scope)
        raise ToolError("insufficient_scope")

    def make_client(_app: Flask, owner_user_id: int) -> RecordingClient:
        client_creations.append(owner_user_id)
        return RecordingClient()

    server, _app, owners, _client = _register(
        tmp_path,
        client_factory=make_client,
        authorize=deny,
    )

    with pytest.raises(ToolError, match="insufficient_scope"):
        asyncio.run(server.call_tool(tool_name, arguments))

    assert requested_scopes == ["mcp.translator"]
    assert client_creations == []
    assert owners == []


def test_cover_source_authority_rejects_outside_root_before_client_lookup(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_source = tmp_path / "outside.png"
    outside_source.write_bytes(b"synthetic source")
    server, _app, owners, _client = _register(
        tmp_path,
        allowed_roots=(str(allowed_root),),
    )

    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "carlina_translator_cover_import",
                {"project_id": "project-a", "source_path": str(outside_source)},
            )
        )

    assert owners == []


def test_start_forwards_normalized_passive_profile_owner_and_policy_pins(
    tmp_path: Path,
) -> None:
    profile = require_executor_profile(_EXECUTOR_PROFILE)
    normalization_calls: list[tuple[str, Mapping[str, Any] | None]] = []
    starts: list[dict[str, Any]] = []

    def normalize(mode: str, value: Mapping[str, Any] | None) -> ExecutorProfile | None:
        normalization_calls.append((mode, value))
        return profile

    def start(
        app: Flask,
        *,
        owner_user_id: int,
        project_id: str,
        mode: str,
        executor_profile: ExecutorProfile | None = None,
        research_dossier_revision_id: str | None = None,
        context_mode: str | None = None,
    ) -> dict[str, Any]:
        starts.append(
            {
                "app": app,
                "owner_user_id": owner_user_id,
                "project_id": project_id,
                "mode": mode,
                "executor_profile": executor_profile,
                "research_dossier_revision_id": research_dossier_revision_id,
                "context_mode": context_mode,
            }
        )
        return {"started": True}

    server, app, _owners, _client = _register(
        tmp_path,
        owner_user_id=901,
        normalize_executor_profile=normalize,
        start_execution=start,
    )
    asyncio.run(
        server.call_tool(
            "carlina_translator_start",
            {
                "project_id": "project-a",
                "mode": " PaSsIvE ",
                "executor_profile": _EXECUTOR_PROFILE,
                "research_dossier_revision_id": "dossier-revision-17",
                "context_mode": "retained",
            },
        )
    )

    assert normalization_calls == [("passive", _EXECUTOR_PROFILE)]
    assert starts == [
        {
            "app": app,
            "owner_user_id": 901,
            "project_id": "project-a",
            "mode": "passive",
            "executor_profile": profile,
            "research_dossier_revision_id": "dossier-revision-17",
            "context_mode": "retained",
        }
    ]


def test_project_review_glossary_and_context_forward_revision_and_export_fences(
    tmp_path: Path,
) -> None:
    client = RecordingClient()
    server, _app, owners, _client = _register(
        tmp_path,
        owner_user_id=802,
        translator_client=client,
    )

    asyncio.run(
        server.call_tool(
            "carlina_translator_export_get",
            {
                "project_id": "project-export",
                "operation_id": "operation-exact",
                "include_content": True,
                "offset": 8192,
                "limit": 4096,
                "expected_sha256": "a" * 64,
            },
        )
    )
    asyncio.run(
        server.call_tool(
            "carlina_translator_review_coverage_record",
            {
                "project_id": "project-review",
                "entries": [
                    {
                        "block_id": "block-a",
                        "expected_target_version": 9,
                        "scope": "block",
                        "source_anchor_ids": [],
                    }
                ],
                "kind": "bilingual",
                "idempotency_key": "coverage-retry-a",
                "reviewer_kind": "human",
            },
        )
    )
    asyncio.run(
        server.call_tool(
            "carlina_translator_shared_glossary_apply",
            {
                "project_id": "project-glossary",
                "asset_id": "asset-a",
                "revision": 12,
                "mode": "update",
                "preview_token": "b" * 64,
                "decisions": [{"id": "term-a", "choice": "incoming"}],
                "idempotency_key": "glossary-retry-b",
            },
        )
    )
    asyncio.run(
        server.call_tool(
            "carlina_translator_context_get",
            {
                "project_id": "project-context",
                "before_block_id": "block-current",
                "max_segments": 2,
                "source_plan_id": "source-plan-pinned",
            },
        )
    )

    assert owners == [802, 802, 802, 802]
    assert client.calls == [
        (
            "export_document_get",
            ("project-export", "operation-exact"),
            {
                "include_content": True,
                "offset": 8192,
                "limit": 4096,
                "expected_sha256": "a" * 64,
            },
        ),
        (
            "review_coverage_record",
            ("project-review",),
            {
                "entries": [
                    {
                        "block_id": "block-a",
                        "expected_target_version": 9,
                        "scope": "block",
                        "source_anchor_ids": [],
                    }
                ],
                "kind": "bilingual",
                "idempotency_key": "coverage-retry-a",
                "reviewer_kind": "human",
                "comment": "",
            },
        ),
        (
            "shared_glossary_apply",
            ("project-glossary",),
            {
                "asset_id": "asset-a",
                "revision": 12,
                "mode": "update",
                "preview_token": "b" * 64,
                "decisions": [{"id": "term-a", "choice": "incoming"}],
                "idempotency_key": "glossary-retry-b",
            },
        ),
        (
            "context_get",
            ("project-context",),
            {
                "before_block_id": "block-current",
                "max_segments": 2,
                "source_plan_id": "source-plan-pinned",
            },
        ),
    ]


@pytest.mark.parametrize(
    ("heading_break", "review_requirement", "expected_options"),
    [
        (False, "none", {}),
        (True, "none", {"start_h2_on_new_page": True}),
        (False, "bilingual", {"review_requirement": "bilingual"}),
        (
            True,
            "bilingual",
            {"start_h2_on_new_page": True, "review_requirement": "bilingual"},
        ),
    ],
)
def test_export_preserves_default_omission_and_combined_optional_flags(
    tmp_path: Path,
    heading_break: bool,
    review_requirement: str,
    expected_options: dict[str, Any],
) -> None:
    server, _app, owners, client = _register(tmp_path, owner_user_id=907)

    asyncio.run(
        server.call_tool(
            "carlina_translator_export",
            {
                "project_id": "project-export",
                "idempotency_key": "export-retry",
                "format": "pdf",
                "start_h2_on_new_page": heading_break,
                "review_requirement": review_requirement,
            },
        )
    )

    assert owners == [907]
    assert client.calls == [
        (
            "export_document",
            ("project-export",),
            {
                "export_format": "pdf",
                "idempotency_key": "export-retry",
                **expected_options,
            },
        )
    ]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, {}),
        ({"epub_cover_mode": "generated"}, {"epub_cover": {"mode": "generated"}}),
        (
            {"epub_cover_mode": "pdf_page", "epub_cover_page": 2},
            {"epub_cover": {"mode": "pdf_page", "page": 2}},
        ),
    ],
)
def test_epub_cover_options_are_optional_and_forwarded(
    tmp_path: Path, options: dict[str, Any], expected: dict[str, Any]
) -> None:
    server, _app, owners, client = _register(tmp_path, owner_user_id=907)
    asyncio.run(
        server.call_tool(
            "carlina_translator_export",
            {"project_id": "book", "idempotency_key": "cover-export", **options},
        )
    )
    assert owners == [907]
    assert client.calls == [
        (
            "export_document",
            ("book",),
            {"export_format": "epub", "idempotency_key": "cover-export", **expected},
        )
    ]


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp"])
def test_epub_publishing_options_are_forwarded(tmp_path: Path, mode: str) -> None:
    server, _app, owners, client = _register(tmp_path, owner_user_id=907)
    options = {"direction": "rtl", "group_endnotes": False}
    asyncio.run(
        server.call_tool(
            "carlina_translator_export",
            {
                "project_id": "book",
                "idempotency_key": "publishing-options",
                "epub_options": options,
                "epub_review_hash": "a" * 64,
                "epub_image_mode": mode,
            },
        )
    )

    assert owners == [907]
    assert client.calls == [
        (
            "export_document",
            ("book",),
            {
                "export_format": "epub",
                "idempotency_key": "publishing-options",
                "epub_options": options,
                "epub_review_hash": "a" * 64,
                "epub_image_mode": mode,
            },
        )
    ]


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp", None])
def test_epub_preflight_arguments_are_forwarded(
    tmp_path: Path, mode: str | None
) -> None:
    review_hash = "d" * 64
    server, _app, owners, client = _register(
        tmp_path,
        owner_user_id=907,
        translator_client=RecordingClient(
            results={"semantic_export_preflight": {"review_hash": review_hash}}
        ),
    )
    options = {"direction": "rtl"}
    cover = {"mode": "generated"}
    result = asyncio.run(
        server.call_tool(
            "carlina_translator_epub_preflight",
            {
                "project_id": "book",
                "epub_options": options,
                "epub_cover": cover,
                "numbering_revision": 4,
                **({"epub_image_mode": mode} if mode is not None else {}),
            },
        )
    )

    assert result.structured_content == {"review_hash": review_hash}
    assert owners == [907]
    assert client.calls == [
        (
            "semantic_export_preflight",
            ("book",),
            {
                "epub_options": options,
                "epub_cover": cover,
                "numbering_revision": 4,
                "epub_image_mode": mode or "jpeg",
            },
        )
    ]


@pytest.mark.parametrize("mode", [None, "preserve", "JPEG", True, 1, []])
@pytest.mark.parametrize(
    "tool_name", ["carlina_translator_export", "carlina_translator_epub_preflight"]
)
def test_epub_image_mode_sdk_validation_precedes_client_creation(
    tmp_path: Path, mode, tool_name
):
    server, _app, owners, client = _register(tmp_path, owner_user_id=907)
    arguments = {"project_id": "book", "epub_image_mode": mode}
    arguments.update(
        {"idempotency_key": "strict-mode"}
        if tool_name.endswith("export")
        else {"epub_options": {}}
    )
    with pytest.raises(ToolError, match="validation"):
        asyncio.run(server.call_tool(tool_name, arguments))
    assert owners == []
    assert client.calls == []


@pytest.mark.parametrize("page", [0, -1, True, 1.5, "2", 1_000_001])
def test_epub_cover_page_sdk_validation_precedes_client_creation(
    tmp_path: Path, page: Any
) -> None:
    server, _app, owners, client = _register(tmp_path)
    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "carlina_translator_export",
                {
                    "project_id": "book",
                    "idempotency_key": "cover-export",
                    "epub_cover_mode": "pdf_page",
                    "epub_cover_page": page,
                },
            )
        )
    assert owners == []
    assert client.calls == []


def test_source_pages_resolve_then_render_and_return_the_image_result(
    tmp_path: Path,
) -> None:
    client = RecordingClient(
        {
            "review_source_pages": {
                "document_id": "source-doc-1",
                "page_numbers": [4, 7],
                "blocks": ["block-4", "block-7"],
            }
        }
    )
    renderer_calls: list[tuple[int, str, list[int], int]] = []
    rendered = {"pages": [{"page_number": 4}, {"page_number": 7}]}
    image_calls: list[dict[str, Any]] = []
    expected_result = CallToolResult(
        content=[TextContent(type="text", text="rendered")],
        structuredContent=rendered,
    )

    def render(
        owner_user_id: int,
        document_id: str,
        page_numbers: list[int],
        *,
        max_edge: int,
    ) -> dict[str, Any]:
        renderer_calls.append((owner_user_id, document_id, page_numbers, max_edge))
        return rendered

    def image(payload: dict[str, Any]) -> CallToolResult:
        image_calls.append(payload)
        return expected_result

    server, _app, owners, _client = _register(
        tmp_path,
        owner_user_id=222,
        translator_client=client,
        source_pages=render,
        image_result=image,
    )
    result = asyncio.run(
        server.call_tool(
            "carlina_translator_review_source_pages",
            {
                "project_id": "project-source",
                "block_ids": ["block-4", "block-7"],
                "max_edge": 1200,
            },
        )
    )

    assert client.calls == [
        (
            "review_source_pages",
            ("project-source",),
            {"block_ids": ["block-4", "block-7"]},
        )
    ]
    assert owners == [222]
    assert renderer_calls == [(222, "source-doc-1", [4, 7], 1200)]
    assert image_calls == [
        {
            "pages": [{"page_number": 4}, {"page_number": 7}],
            "translator_project_id": "project-source",
            "translator_blocks": ["block-4", "block-7"],
        }
    ]
    assert result is expected_result


def test_runtime_failure_is_normalized_after_client_error(tmp_path: Path) -> None:
    client = RecordingClient({"mcp_job_projection": ValueError("synthetic failure")})
    server, _app, _owners, _client = _register(
        tmp_path,
        translator_client=client,
    )

    with pytest.raises(ToolError, match="normalized ValueError: synthetic failure"):
        asyncio.run(
            server.call_tool(
                "carlina_translator_get",
                {"project_id": "project-error"},
            )
        )


def test_create_defers_planning_and_keeps_passive_preanalysis_disabled(
    tmp_path: Path,
) -> None:
    client = RecordingClient(
        {"create_from_document": {"id": "project-created", "status": "planning"}}
    )
    server, _app, owners, _client = _register(
        tmp_path,
        owner_user_id=419,
        translator_client=client,
    )

    asyncio.run(
        server.call_tool(
            "carlina_translator_create",
            {
                "name": "Synthetic project",
                "target_lang": "en",
                "document_id": "source-doc-2",
            },
        )
    )

    assert owners == [419]
    assert client.calls == [
        (
            "create_from_document",
            ("source-doc-2",),
            {
                "name": "Synthetic project",
                "source_lang": "Auto-detect",
                "target_lang": "en",
                "idempotency_key": None,
                "defer_source_planning": True,
                "execution_mode": "passive",
                "enable_pre_analysis": False,
            },
        )
    ]
