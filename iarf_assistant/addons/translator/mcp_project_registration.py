from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from flask import Flask
from pydantic import Field

from ...core.mcp_tool_runtime import McpToolRuntime
from .mcp_execution import create_mcp_project
from .mcp_settings import ExportMetadataSettings, TranslatorSettings

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from ...core.executor_profile import ExecutorProfile
    from ...models import McpExecution
    from .api_client import TranslatorClient
    from .mcp_registration_types import TranslatorExecutionStarter


def register_project_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    client: Callable[[Flask, int], TranslatorClient],
    start_execution: TranslatorExecutionStarter,
    normalize_executor_profile: Callable[
        [str, Mapping[str, Any] | None], ExecutorProfile | None
    ],
    execution_payload: Callable[[McpExecution], dict[str, Any]],
) -> None:
    @server.tool(
        name="carlina_translator_create",
        description=(
            "Create an owner-scoped document-only Translator project from a completed "
            "Document Processor document. Returns after snapshot capture and durable job creation; "
            "segmentation/planning runs in the visible background job. Retry with the same idempotency_key; "
            "without a key, an identical request and source snapshot reuse one project. A new explicit key "
            "creates a separate project. Inspect planning status before start. Repair source page continuations "
            "with carlina_document_repair_boundaries before creation. Call start separately with a managed "
            "model or declared, task-authorized passive executor profile."
        ),
    )
    async def translator_create(
        name: str,
        target_lang: str,
        document_id: str,
        source_lang: str = "Auto-detect",
        settings: TranslatorSettings | None = None,
        mode: Literal["passive", "managed"] = "passive",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")

        def run() -> dict[str, Any]:
            options = settings.model_dump(exclude_none=True) if settings else {}
            return create_mcp_project(
                owner_user_id=principal.owner_user_id,
                document_id=document_id,
                name=name,
                source_lang=source_lang,
                target_lang=target_lang,
                mode=mode,
                idempotency_key=idempotency_key,
                settings=options,
                client_factory=lambda: client(runtime.app, principal.owner_user_id),
                execution_payload=execution_payload,
            )

        try:
            return await runtime.run(run)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_start",
        description=(
            "Start the planned document-only semantic-block translation. Managed mode "
            "requires a ready Carlina model; passive mode requires a user-confirmed "
            "executor_profile and uses the lease/submit work loop. Optionally pin one "
            "exact external research dossier revision with research_dossier_revision_id. "
            "For passive runs, context_mode='retained' omits preceding prose already in this "
            "conversation; 'self_contained' supplies a bounded excerpt for a fresh executor. "
            "Omitting context_mode uses the saved policy (default self_contained). The override "
            "is per-run; managed calls always use self_contained."
        ),
    )
    async def translator_start(
        project_id: str,
        mode: str = "passive",
        executor_profile: dict[str, Any] | None = None,
        research_dossier_revision_id: str | None = None,
        context_mode: Literal["self_contained", "retained"] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            normalized_mode = str(mode or "").strip().lower()
            normalized_profile = normalize_executor_profile(
                normalized_mode, executor_profile
            )
            return await runtime.run(
                partial(
                    start_execution,
                    runtime.app,
                    owner_user_id=principal.owner_user_id,
                    project_id=project_id,
                    mode=normalized_mode,
                    executor_profile=normalized_profile,
                    research_dossier_revision_id=research_dossier_revision_id,
                    context_mode=context_mode,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_get",
        description=(
            "Read a safe owner-scoped document-block Translator projection; use "
            "include_output=true to retrieve verified semantic target HTML after completion, "
            "include_glossary=true to read saved terms and reviewable suggestions, "
            "or include_export_metadata=true to read the saved export defaults and version."
        ),
    )
    async def translator_get(
        project_id: str,
        include_output: bool = False,
        include_glossary: bool = False,
        include_export_metadata: bool = False,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).mcp_job_projection(
                    project_id,
                    include_output=include_output,
                    include_glossary=include_glossary,
                    include_export_metadata=include_export_metadata,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_epub_edition_get",
        description="Read saved EPUB choices for the owner's translation, including version and current_source_revision witnesses. Source plans remain immutable.",
    )
    async def translator_epub_edition_get(project_id: str) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).epub_edition_get(
                    project_id
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_epub_edition_save",
        description="Save reusable EPUB choices separately from the owner's translation source plan. Supply current edition version and source witness; a fresh preflight remains necessary for export.",
    )
    async def translator_epub_edition_save(
        project_id: str,
        recipe: dict[str, Any],
        expected_version: Annotated[int, Field(strict=True, ge=0)],
        expected_source_revision: Annotated[
            str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")
        ],
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).epub_edition_save(
                    project_id,
                    recipe=recipe,
                    expected_version=expected_version,
                    expected_source_revision=expected_source_revision,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_epub_edition_clear",
        description="Clear saved EPUB choices for the owner's translation using their current version. Source material and prior exports remain intact.",
    )
    async def translator_epub_edition_clear(
        project_id: str, expected_version: Annotated[int, Field(strict=True, ge=0)]
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).epub_edition_clear(
                    project_id, expected_version=expected_version
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_epub_preflight",
        description=(
            "Review the owner's current translated EPUB target, saved edition metadata, and cover "
            "choice. Returns source-free quality findings, outline, and the exact semantic "
            "input hash to bind to the following export. This read-only call creates no "
            "artifact and makes no model call."
        ),
    )
    async def translator_epub_preflight(
        project_id: str,
        epub_options: dict[str, Any],
        epub_image_mode: Literal["auto", "jpeg", "webp"] = "jpeg",
        epub_cover: dict[str, Any] | None = None,
        numbering_revision: Annotated[int | None, Field(strict=True, ge=0)] = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).semantic_export_preflight(
                    project_id,
                    epub_options=epub_options,
                    epub_image_mode=epub_image_mode,
                    epub_cover=epub_cover,
                    numbering_revision=numbering_revision,
                )
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_export",
        description=(
            "Start an owner-scoped EPUB, DOCX, PDF, or HTML export of a document translation "
            "using saved edition metadata and cover. No model call. Supply a stable "
            "idempotency_key for retries, then poll the returned operation_id with "
            "carlina_translator_export_get. Active project work may block admission."
        ),
    )
    async def translator_export(
        project_id: str,
        idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
        format: Literal["epub", "docx", "html", "pdf"] = "epub",
        start_h2_on_new_page: Annotated[
            bool,
            Field(
                strict=True,
                description="Start each H2 on a new page in DOCX/PDF; useful for chapters or letters.",
            ),
        ] = False,
        review_requirement: Literal[
            "none", "structural", "target_reading", "bilingual"
        ] = "none",
        epub_options: dict[str, Any] | None = None,
        epub_review_hash: str | None = None,
        # Omission reuses the saved mode; the Literal rejects explicit null.
        epub_image_mode: Literal["auto", "jpeg", "webp"] = cast(
            Literal["auto", "jpeg", "webp"], None
        ),
        epub_cover_mode: Literal["existing", "pdf_page", "generated"] = "existing",
        epub_cover_page: Annotated[
            int | None,
            Field(
                strict=True,
                ge=1,
                le=1_000_000,
                description="1-based physical source PDF page for an EPUB cover.",
            ),
        ] = None,
        epub_cover_crop: list[float] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:

            def run_export() -> dict[str, Any]:
                optional_options: dict[str, Any] = {}
                if cast(object, epub_image_mode) is not None:
                    optional_options["epub_image_mode"] = epub_image_mode
                if start_h2_on_new_page:
                    optional_options["start_h2_on_new_page"] = True
                if review_requirement != "none":
                    optional_options["review_requirement"] = review_requirement
                if epub_options is not None:
                    optional_options["epub_options"] = epub_options
                if epub_review_hash is not None:
                    optional_options["epub_review_hash"] = epub_review_hash
                if (
                    epub_cover_mode != "existing"
                    or epub_cover_page is not None
                    or epub_cover_crop is not None
                ):
                    optional_options["epub_cover"] = {
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
                return client(runtime.app, principal.owner_user_id).export_document(
                    project_id,
                    export_format=format,
                    idempotency_key=idempotency_key,
                    **optional_options,
                )

            return await runtime.run(run_export)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_export_get",
        description=(
            "Read one exact owner-scoped document export operation. Poll until ready; "
            "metadata is returned without file bytes by default. With include_content=true, "
            "retrieve at most 65536 bytes per base64 chunk, follow next_offset until eof, "
            "and use expected_sha256 to pin the artifact. Verify the assembled file's "
            "byte_length and SHA-256 against the creation witness. No server paths are exposed."
        ),
    )
    async def translator_export_get(
        project_id: str,
        operation_id: str,
        include_content: Annotated[bool, Field(strict=True)] = False,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=65536)] = 65536,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).export_document_get(
                    project_id,
                    operation_id,
                    include_content=include_content,
                    offset=offset,
                    limit=limit,
                    expected_sha256=expected_sha256,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_delete_project",
        description=(
            "Irreversibly delete one owner-scoped terminal Translator project and its project-owned "
            "data. The host must obtain explicit user confirmation before calling this tool. "
            "Pass confirmation='delete' and a fresh idempotency key. Active Translator operations "
            "or nonterminal MCP executions/work leases are refused; retry only after they become terminal."
        ),
    )
    async def translator_delete_project(
        project_id: str,
        idempotency_key: str,
        confirmation: Literal["delete"],
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).delete_project(
                    project_id,
                    idempotency_key=idempotency_key,
                    confirmation=confirmation,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_cover_import",
        description=(
            "Authorize one server-local PNG or JPEG source path, capture it as a "
            "bounded Translator export cover, and return its reusable canonical "
            "cover_upload_path. The project is checked first; this does not modify "
            "project metadata. Pass that path to the full-replacement metadata update."
        ),
    )
    async def translator_cover_import(
        project_id: str,
        source_path: str,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")

        def run() -> dict[str, Any]:
            from ...core.mcp_jobs import authorize_source_path

            path = authorize_source_path(principal, source_path)
            return client(runtime.app, principal.owner_user_id).prepare_export_cover(
                project_id,
                path,
            )

        try:
            return await runtime.run(run)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_export_metadata_update",
        description=(
            "Replace the owner-scoped saved export metadata using a compare-and-set "
            "version. The full-replacement metadata supports title, subtitle, "
            "description, publisher, publication_date, rights, journal_or_series, "
            "volume, authors, editors, translators, other_contributors, subjects, "
            "identifiers, and cover_upload_path; language remains project-controlled. "
            "Omitted fields are cleared, [] clears lists, and an empty optional string "
            "clears that value; pass {} to clear everything. Prepare a cover with "
            "carlina_translator_cover_import first. An identical retry is safe, while "
            "active Translator work and stale versions are rejected."
        ),
    )
    async def translator_export_metadata_update(
        project_id: str,
        metadata: ExportMetadataSettings,
        expected_version: int,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).export_metadata_update(
                    project_id,
                    metadata=metadata.model_dump(exclude_none=True),
                    expected_version=expected_version,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_list",
        description=(
            "List owner-scoped translation projects in Translator. Filter by status "
            "(e.g. 'completed', 'in_progress') to inspect progress, segments, and "
            "completed target documents."
        ),
    )
    async def translator_list(
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")

        def run() -> dict[str, Any]:
            project_client = client(runtime.app, principal.owner_user_id)
            projects = project_client.list_projects(limit=limit)
            if status:
                normalized = str(status).strip().lower()
                projects = [
                    p
                    for p in projects
                    if str(p.get("status") or "").strip().lower() == normalized
                ]
            return {"projects": projects}

        try:
            return await runtime.run(run)
        except BaseException as exc:
            raise runtime.failure(exc) from exc
