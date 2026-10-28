"""MCP tool registration owned by the Document Processor product."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, cast

from flask import Flask
from pydantic import Field

from ...core.executor_profile import ExecutorProfile, ExecutorProfileInput
from ...core.mcp_tool_runtime import McpToolRuntime
from .batching import DocumentBatchPlanInput, normalize_document_batch_plan
from .mcp_issue_registration import register_issue_tools
from .mcp_recovery_registration import register_recovery_tools
from .mcp_review_registration import register_review_tools
from .mcp_settings import DocumentSettings, document_settings_to_dict

if TYPE_CHECKING:
    from mcp.server import MCPServer
    from mcp_types import CallToolResult

    from .mcp_service import DocProcessorMcpService


class DocumentExecutionStarter(Protocol):
    """Injected Document Processor execution startup callback."""

    def __call__(
        self,
        app: Flask,
        *,
        owner_user_id: int,
        document_id: str,
        mode: str,
        executor_profile: ExecutorProfile | None = None,
        batch_plan: Mapping[str, Any] | None = None,
        restart_from_scratch: bool = False,
    ) -> dict[str, Any]: ...


def register_document_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    service: Callable[[Flask, int], DocProcessorMcpService],
    start_execution: DocumentExecutionStarter,
    normalize_executor_profile: Callable[
        [str, ExecutorProfileInput | None], ExecutorProfile | None
    ],
    compact_start: Callable[[dict[str, Any]], dict[str, Any]],
    image_result: Callable[[dict[str, Any]], CallToolResult],
    review_edit_failure: Callable[[BaseException, str, str], CallToolResult | None],
) -> None:
    """Register the Document Processor MCP tools on the official SDK server."""

    @server.tool(
        name="carlina_document_import",
        description=(
            "Copy one authority-scoped local document into Document Processor. "
            "This does not start processing; configure and start it explicitly."
        ),
    )
    async def document_import(
        source_path: str,
        settings: DocumentSettings | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")

        def run() -> dict[str, Any]:
            from iarf_assistant.core.mcp_jobs import authorize_source_path

            path = authorize_source_path(principal, source_path)
            return service(runtime.app, principal.owner_user_id).import_document(
                path,
                settings=document_settings_to_dict(settings),
            )

        try:
            return await runtime.run(run)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_configure",
        description="Validate and persist Document Processor settings before starting.",
    )
    async def document_configure(
        document_id: str,
        settings: DocumentSettings,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(
                    runtime.app, principal.owner_user_id
                ).configure_document(document_id, document_settings_to_dict(settings))
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_start",
        description=(
            "Start a configured document with Carlina-managed providers or passive "
            "MCP completion dispatch. For task-authorized passive work, declare an executor_profile "
            "envelope and a complete batch_plan. Each new batch is one multipage "
            "JSON completion. Passive PDFs use full pages, ignoring saved crop boxes; only later "
            "image-coordinate review may require the Carlina UI. Impossible metadata image "
            "budgets are rejected before queuing. include_details=true echoes the plan and settings."
            " Saved partial PDF checkpoints require explicit restart_from_scratch=true to discard; "
            "a new passive execution does not resume them. Use carlina_job_resume for a "
            "suspended execution to retain its accepted work instead."
        ),
    )
    async def document_start(
        document_id: str,
        mode: str = "passive",
        executor_profile: ExecutorProfileInput | None = None,
        batch_plan: DocumentBatchPlanInput | None = None,
        include_details: bool = False,
        restart_from_scratch: bool = False,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            normalized_mode = str(mode or "").strip().lower()
            normalized_profile = normalize_executor_profile(
                normalized_mode, executor_profile
            )
            if normalized_mode == "managed" and batch_plan is not None:
                raise ValueError("batch_plan is only accepted for passive execution.")
            if normalized_mode == "managed" and restart_from_scratch:
                raise ValueError(
                    "restart_from_scratch is only accepted for passive execution."
                )

            try:
                normalized_batch_plan = normalize_document_batch_plan(batch_plan)
            except (TypeError, ValueError) as exc:
                from .mcp_service import DocProcessorMcpServiceError

                raise DocProcessorMcpServiceError(
                    "Passive PDF batch_plan is invalid; derive its ranges from source.page_count.",
                    code="invalid_batch_plan",
                ) from exc
            result = await runtime.run(
                lambda: start_execution(
                    runtime.app,
                    owner_user_id=principal.owner_user_id,
                    document_id=document_id,
                    mode=normalized_mode,
                    executor_profile=normalized_profile,
                    batch_plan=normalized_batch_plan,
                    restart_from_scratch=restart_from_scratch,
                )
            )
            if include_details:
                return result
            return compact_start(result)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_get",
        description="Read one owner-scoped document and optional current semantic output. Completed output includes its review revision; use expected_revision to reject stale snapshots. After edits, fetch anew rather than reusing a cached export or tool result.",
    )
    async def document_get(
        document_id: str,
        include_output: bool = False,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(runtime.app, principal.owner_user_id).get_document(
                    document_id,
                    include_output=include_output,
                    expected_revision=expected_revision,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_epub_edition_get",
        description="Read saved EPUB edition choices and their source/version witnesses for an owned book. Changed source clears effective anchor-based choices.",
    )
    async def document_epub_edition_get(document_id: str) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(runtime.app, principal.owner_user_id).epub_edition_get(
                    document_id
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_epub_edition_save",
        description="Save reusable EPUB choices separately from an owned extraction. Supply the exact edition version and current_source_revision from readback or preflight. Export still requires fresh review.",
    )
    async def document_epub_edition_save(
        document_id: str,
        recipe: dict[str, Any],
        expected_version: Annotated[int, Field(strict=True, ge=0)],
        expected_source_revision: Annotated[
            str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")
        ],
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(runtime.app, principal.owner_user_id).epub_edition_save(
                    document_id,
                    recipe=recipe,
                    expected_version=expected_version,
                    expected_source_revision=expected_source_revision,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_epub_edition_clear",
        description="Clear saved EPUB choices for an owned book using their current version. Source content and prior exports remain intact.",
    )
    async def document_epub_edition_clear(
        document_id: str, expected_version: Annotated[int, Field(strict=True, ge=0)]
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(
                    runtime.app, principal.owner_user_id
                ).epub_edition_clear(document_id, expected_version=expected_version)
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_epub_preflight",
        description=(
            "Inspect bounded EPUB structure, outline, and source-free quality findings "
            "for an owned completed extraction. This read-only call does not build an "
            "artifact, use a model, or fetch document resources. Pass expected_revision "
            "to reject stale evidence."
        ),
    )
    async def document_epub_preflight(
        document_id: str,
        epub_options: dict[str, Any] | None = None,
        epub_image_mode: Literal["auto", "jpeg", "webp"] = "jpeg",
        epub_cover_mode: Literal["existing", "pdf_page", "generated"] = "existing",
        epub_cover_page: Annotated[
            int | None,
            Field(
                strict=True,
                ge=1,
                le=1_000_000,
                description="1-based physical PDF page for a PDF-page EPUB cover.",
            ),
        ] = None,
        expected_revision: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
        | None = None,
        epub_cover_crop: list[float] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(runtime.app, principal.owner_user_id).epub_preflight(
                    document_id,
                    epub_options=epub_options,
                    epub_image_mode=epub_image_mode,
                    **(
                        {
                            "epub_cover": {
                                "mode": epub_cover_mode,
                                **(
                                    {"page": epub_cover_page}
                                    if epub_cover_page is not None
                                    else {}
                                ),
                                **(
                                    {"crop": epub_cover_crop}
                                    if epub_cover_crop is not None
                                    else {}
                                ),
                            }
                        }
                        if epub_cover_mode != "existing"
                        or epub_cover_page is not None
                        or epub_cover_crop is not None
                        else {}
                    ),
                    expected_revision=expected_revision,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_export",
        description=(
            "Start an owner-scoped EPUB, DOCX, PDF, HTML, or Markdown export of a completed "
            "extraction without a model call. Supply a fresh document revision as expected_revision "
            "and a stable idempotency_key. Poll the exact returned operation_id with "
            "carlina_document_export_get. Source text changes before rendering reject the job."
        ),
    )
    async def document_export(
        document_id: str,
        idempotency_key: Annotated[
            str, Field(strict=True, min_length=1, max_length=128)
        ],
        expected_revision: Annotated[
            str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")
        ],
        format: Literal["epub", "docx", "html", "pdf", "md"] = "epub",
        start_h2_on_new_page: Annotated[
            bool,
            Field(strict=True, description="Start each H2 on a new page in DOCX/PDF."),
        ] = False,
        epub_cover_mode: Literal["existing", "pdf_page", "generated"] = "existing",
        epub_cover_page: Annotated[
            int | None,
            Field(
                strict=True,
                ge=1,
                le=1_000_000,
                description="1-based physical PDF page for a PDF-page EPUB cover.",
            ),
        ] = None,
        epub_options: dict[str, Any] | None = None,
        # Omission reuses the saved mode; the Literal rejects explicit null.
        epub_image_mode: Literal["auto", "jpeg", "webp"] = cast(
            Literal["auto", "jpeg", "webp"], None
        ),
        epub_review_hash: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
        | None = None,
        epub_cover_crop: list[float] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(runtime.app, principal.owner_user_id).export_document(
                    document_id,
                    export_format=format,
                    idempotency_key=idempotency_key,
                    expected_revision=expected_revision,
                    start_h2_on_new_page=start_h2_on_new_page,
                    **(
                        {"epub_image_mode": epub_image_mode}
                        if cast(object, epub_image_mode) is not None
                        else {}
                    ),
                    **(
                        {
                            "epub_cover": {
                                "mode": epub_cover_mode,
                                **(
                                    {"page": epub_cover_page}
                                    if epub_cover_page is not None
                                    else {}
                                ),
                                **(
                                    {"crop": epub_cover_crop}
                                    if epub_cover_crop is not None
                                    else {}
                                ),
                            }
                        }
                        if epub_cover_mode != "existing"
                        or epub_cover_page is not None
                        or epub_cover_crop is not None
                        else {}
                    ),
                    **(
                        {"epub_options": epub_options}
                        if epub_options is not None
                        else {}
                    ),
                    **(
                        {"epub_review_hash": epub_review_hash}
                        if epub_review_hash is not None
                        else {}
                    ),
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_export_get",
        description=(
            "Read one exact owner-scoped extraction export operation. Poll until ready; "
            "metadata has no file bytes by default. With include_content=true, retrieve at most "
            "65536 bytes per base64 chunk and follow next_offset until eof. Pin expected_sha256 "
            "and verify the assembled byte_length and SHA-256. No server paths are exposed."
        ),
    )
    async def document_export_get(
        document_id: str,
        operation_id: str,
        include_content: Annotated[bool, Field(strict=True)] = False,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=65536)] = 65536,
        expected_sha256: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
        | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: service(
                    runtime.app, principal.owner_user_id
                ).export_document_get(
                    document_id,
                    operation_id,
                    include_content=include_content,
                    offset=offset,
                    limit=limit,
                    expected_sha256=expected_sha256,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_list",
        description=(
            "List owner-scoped documents in Document Processor. Filter by status "
            "(e.g. 'completed' for extracted documents, 'reviewing_images' or "
            "'needs_cropping' for documents awaiting review) to find documents "
            "ready for translation or inspection. Returns compact identity/status rows; "
            "page with offset/limit and use document_get for settings and metadata."
        ),
    )
    async def document_list(
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")

        def run() -> dict[str, Any]:
            page_limit = min(100, max(1, limit))
            page_offset = max(0, offset)
            documents = service(runtime.app, principal.owner_user_id).list_documents(
                status=status,
                limit=page_limit + 1,
                offset=page_offset,
            )
            return {
                "documents": [
                    {
                        key: document[key]
                        for key in (
                            "id",
                            "filename",
                            "title",
                            "status",
                            "language",
                            "created_at",
                            "has_output",
                            "review_kind",
                        )
                        if key in document
                    }
                    for document in documents[:page_limit]
                ],
                "pagination": {
                    "offset": page_offset,
                    "limit": page_limit,
                    "next_offset": (
                        page_offset + page_limit
                        if len(documents) > page_limit
                        else None
                    ),
                },
            }

        try:
            return await runtime.run(run)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    register_recovery_tools(server, runtime, service=service)
    register_issue_tools(server, runtime, service=service)
    register_review_tools(
        server,
        runtime,
        service=service,
        image_result=image_result,
        review_edit_failure=review_edit_failure,
    )
