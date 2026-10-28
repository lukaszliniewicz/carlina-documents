"""Document Processor MCP review and repair registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from flask import Flask
from mcp_types import CallToolResult
from pydantic import Field

from ...core.mcp_tool_runtime import McpToolRuntime
from .mcp_settings import DocumentNoteRepair, DocumentReviewEdit

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .mcp_review import DocumentReviewService
    from .mcp_service import DocProcessorMcpService


def register_review_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    service: Callable[[Flask, int], DocProcessorMcpService],
    image_result: Callable[[dict[str, Any]], CallToolResult],
    review_edit_failure: Callable[[BaseException, str, str], CallToolResult | None],
) -> None:
    def document_review(owner_user_id: int) -> DocumentReviewService:
        from .mcp_review import DocumentReviewService

        return DocumentReviewService(service(runtime.app, owner_user_id))

    @server.tool(
        name="carlina_document_review_outline",
        description="Read a compact heading hierarchy, heading levels, physical page references and diagnostic hints from completed extraction. Hints do not establish source correctness. IDs are revision-scoped; reuse the returned revision for subsequent calls.",
    )
    async def document_review_outline(
        document_id: str,
        offset: int = 0,
        limit: int = 100,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).read(
                    document_id,
                    "outline",
                    expected_revision=expected_revision,
                    offset=offset,
                    limit=limit,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_inventory",
        description="Read a revision-fenced, paginated inventory of all distinct visible tokens, occurrence counts and bounded element/page examples. Scope includes body, headings or notes. Use for systematic dictionary/ASCII-folding triage and rerun on final current text. No dictionary or automatic spelling changes; printed oddities must be checked visually. Follow next_offset to finish; overlong tokens are explicitly flagged. Coverage totals are inventory, not proof of completed review.",
    )
    async def document_review_inventory(
        document_id: str,
        offset: int = 0,
        limit: int = 100,
        scope: Literal["all", "body", "headings", "notes"] = "all",
        query: str = "",
        case_sensitive: Annotated[bool, Field(strict=True)] = False,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).read(
                    document_id,
                    "inventory",
                    expected_revision=expected_revision,
                    offset=offset,
                    limit=limit,
                    scope=scope,
                    query=query,
                    case_sensitive=case_sensitive,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_search",
        description="Search current extraction for literal text (not regex), with strict boolean case_sensitive and optional Unicode whole_word matching. Scope/page filters restrict hits, not neighboring context. Returns effective search options, actual matched text and heading ancestry. total_matches counts matching elements, not occurrences. include_html adds bounded complete markup or html_omitted. Follow next_offset with expected_revision.",
    )
    async def document_review_search(
        document_id: str,
        query: str,
        case_sensitive: Annotated[bool, Field(strict=True)] = False,
        whole_word: Annotated[bool, Field(strict=True)] = False,
        before: int = 0,
        after: int = 0,
        offset: int = 0,
        limit: int = 20,
        expected_revision: str | None = None,
        scope: Literal["all", "body", "headings", "notes"] = "all",
        page_number: int | None = None,
        include_html: bool = False,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).read(
                    document_id,
                    "search",
                    expected_revision=expected_revision,
                    query=query,
                    scope=scope,
                    page_number=page_number,
                    include_html=include_html,
                    case_sensitive=case_sensitive,
                    whole_word=whole_word,
                    before=before,
                    after=after,
                    offset=offset,
                    limit=limit,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_fetch",
        description="Fetch full editable HTML/text by revision-scoped element IDs or paginated document order, with scope/page filters, heading ancestry and up to three neighbors before/after. At most 20 main elements and 64k main HTML characters; use smaller descendants for oversized containers. Context HTML is separately bounded and explicitly omitted, never silently truncated.",
    )
    async def document_review_fetch(
        document_id: str,
        element_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 20,
        expected_revision: str | None = None,
        scope: Literal["all", "body", "headings", "notes"] = "all",
        page_number: int | None = None,
        before: int = 0,
        after: int = 0,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).read(
                    document_id,
                    "fetch",
                    expected_revision=expected_revision,
                    element_ids=element_ids,
                    scope=scope,
                    page_number=page_number,
                    before=before,
                    after=after,
                    offset=offset,
                    limit=limit,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_notes",
        description="Inspect a paginated inventory of footnote/endnote bodies, references, backlinks and bounded diagnostic hints. include_html returns complete bounded body markup or html_omitted. Reference page provenance is physical, note placement is not inferred. May diagnose invalid semantic references without permitting edits to invalid output.",
    )
    async def document_review_notes(
        document_id: str,
        offset: int = 0,
        limit: int = 20,
        include_html: bool = False,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).read(
                    document_id,
                    "notes",
                    expected_revision=expected_revision,
                    offset=offset,
                    limit=limit,
                    include_html=include_html,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_boundaries",
        description="Inspect text around retained physical PDF page starts, including joins inside a paragraph. page_number is the physical page beginning at the boundary. Defaults to text only. Optional images require one page_number and show that source page plus its physical predecessor; omitted output markers do not prove a source gap.",
    )
    async def document_review_boundaries(
        document_id: str,
        page_number: int | None = None,
        offset: int = 0,
        limit: int = 20,
        context_chars: int = 500,
        include_images: bool = False,
        expected_revision: str | None = None,
    ) -> CallToolResult:
        principal = runtime.authorize("mcp.doc_processor")

        def run() -> dict[str, Any]:
            service = document_review(principal.owner_user_id)
            source_page_numbers: list[int] | None = None
            if include_images:
                if page_number is None or page_number < 2:
                    from .mcp_review import DocumentReviewError

                    raise DocumentReviewError(
                        "Boundary images require one physical page_number of at least 2.",
                        code="invalid_review_request",
                    )
                source_page_numbers = [page_number - 1, page_number]
            payload = service.read(
                document_id,
                "boundaries",
                expected_revision=expected_revision,
                page_number=page_number,
                offset=offset,
                limit=limit,
                context_chars=context_chars,
            )
            if source_page_numbers is not None:
                payload["source_pages"] = service.source_pages(
                    document_id, source_page_numbers
                )["pages"]
            return payload

        try:
            return image_result(await runtime.run(run))
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_source_pages",
        description="Fetch 1-4 physical PDF pages as native MCP image attachments. Call directly or forward ImageContent unchanged to the model: serializing the result as text loses visual access. Images returned does not mean images visually inspected. Page numbers are one-based, not printed labels. include_review_context adds bounded current heading evidence with a revision fence; default source-only mode works independently of extraction state.",
    )
    async def document_source_pages(
        document_id: str,
        page_numbers: Annotated[list[int], Field(min_length=1, max_length=4)],
        max_edge: int = 1600,
        include_review_context: Annotated[bool, Field(strict=True)] = False,
        expected_revision: str | None = None,
    ) -> CallToolResult:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            payload = await runtime.run(
                lambda: document_review(principal.owner_user_id).source_pages(
                    document_id,
                    page_numbers,
                    max_edge=max_edge,
                    include_review_context=include_review_context,
                    expected_revision=expected_revision,
                ),
            )
            return image_result(payload)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_repair_boundaries",
        description=(
            "Preview physical-page continuation candidates before creating a Translator project. "
            "Defaults to dry_run=true: inspect boundary_repair.candidates with stable IDs, page numbers "
            "and suggested actions. Nothing is auto-joined. Pass 1-50 decisions [{id, action}], where "
            "action is join_with_space, dehyphenate or keep_hyphen; printed hyphens may be intentional. "
            "Saving requires expected_revision from preview and a stable idempotency_key. "
            "Page anchors and references survive; headings, notes, lists and other protected contexts "
            "are not crossed. One batch is one encrypted undo record. Existing Translator snapshots "
            "stay unchanged. Remove marked page furniture separately when it blocks adjacency."
        ),
    )
    async def document_repair_boundaries(
        document_id: str,
        decisions: list[dict[str, str]] | None = None,
        page_numbers: list[int] | None = None,
        offset: int = 0,
        limit: int = 50,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).repair_boundaries(
                    document_id,
                    decisions=decisions,
                    page_numbers=page_numbers,
                    offset=offset,
                    limit=limit,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_repair_note_reference",
        description=(
            "Restore one visually verified missing note marker to an existing note body. "
            "Select the body element_id and note_element_id from current review evidence. "
            "after_text must match exactly once in unprotected body text; the literal label "
            "is inserted immediately after it. Defaults to dry_run=true. Requires the reviewed "
            "expected_revision and a stable idempotency_key. Read the preview before saving. "
            "Existing IDs, references, note bodies and page markers survive; one encrypted undo "
            "record permits reversal. Unreferenced biographies alone are not evidence of a missing marker."
        ),
    )
    async def document_repair_note_reference(
        document_id: str,
        element_id: str,
        note_element_id: str,
        after_text: Annotated[str, Field(min_length=1, max_length=1000)],
        label: Annotated[str, Field(min_length=1, max_length=64)],
        expected_revision: str,
        idempotency_key: str,
        dry_run: Annotated[bool, Field(strict=True)] = True,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).repair_note_reference(
                    document_id,
                    element_id=element_id,
                    note_element_id=note_element_id,
                    after_text=after_text,
                    label=label,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_note_references",
        description=(
            "List existing note links and bare superscript candidates with revision-bound "
            "element_id, zero-based marker_index, label, href and source context. A bare "
            "superscript may be an exponent: verify against source pages before linking it. "
            "Use review_notes and review_search to locate exact destination bodies."
        ),
    )
    async def document_review_note_references(
        document_id: str,
        offset: int = 0,
        limit: int = 50,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).read(
                    document_id,
                    "note_references",
                    offset=offset,
                    limit=limit,
                    expected_revision=expected_revision,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_repair_note_topology",
        description=(
            "Preview or apply 1-50 witnessed source-note repairs. promote_note marks a leaf "
            "list item, paragraph or aside as a note with label and numbering scope_id; "
            "set_note_scope scopes an existing note; link_reference links an exact marker "
            "from review_note_references to note_element_id, checking expected_label and "
            "expected_href (null for a bare superscript). Promotions in the same batch are "
            "valid destinations. Source prose stays in place; existing backlinks follow "
            "retargeted references. Read the default dry-run preview before saving with "
            "the same expected_revision and idempotency_key. One encrypted undo record. "
            "Existing Translator snapshots and translations remain unchanged."
        ),
    )
    async def document_repair_note_topology(
        document_id: str,
        operations: Annotated[
            list[DocumentNoteRepair], Field(min_length=1, max_length=50)
        ],
        expected_revision: str,
        idempotency_key: str,
        dry_run: Annotated[bool, Field(strict=True)] = True,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).repair_note_topology(
                    document_id,
                    operations=[item.model_dump() for item in operations],
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_cleanup",
        description=(
            "Preview/remove explicitly marked page_header/page_footer elements. Defaults to dry_run=true; "
            "returns revision, counts, bounded examples and protected/skipped items. roles selects header/footer; "
            "optional page_numbers selects physical pages. No text heuristics. Saving requires expected_revision "
            "from preview and stable idempotency_key. Atomic, with encrypted review undo. Page anchors, images, "
            "notes and referenced targets are protected. Exports and existing Translator snapshots are unchanged."
        ),
    )
    async def document_cleanup(
        document_id: str,
        roles: list[str] | None = None,
        page_numbers: list[int] | None = None,
        expected_revision: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).cleanup(
                    document_id,
                    roles=roles,
                    page_numbers=page_numbers,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_apply",
        description="Apply 1-50 atomic scoped edits: heading_level, demote_heading=true (emphasized paragraph preserving IDs/markup), replacement_html, replace_text, replace_texts (1-50 per element), or guarded delete_element. Replacements use case-sensitive literals and optional whole_word; all match counts refer to original reviewed text, never intermediate replacements. Overlapping spans fail. Protected IDs/pages/images/references survive. Requires expected_revision and stable idempotency_key. dry_run previews without writing; rejected batches return per-edit diagnostics with zero-based indexes and actual counts. Read back the new revision; exports and existing Translator snapshots stay unchanged.",
    )
    async def document_review_apply(
        document_id: str,
        expected_revision: str,
        idempotency_key: str,
        edits: Annotated[list[DocumentReviewEdit], Field(min_length=1, max_length=50)],
        dry_run: Annotated[bool, Field(strict=True)] = False,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).apply(
                    document_id,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    edits=edits,
                    dry_run=dry_run,
                ),
            )
        except BaseException as exc:
            detailed = review_edit_failure(exc, document_id, expected_revision)
            if detailed is not None:
                # The SDK accepts an explicit error envelope under this dict schema.
                # Remove this exception when its typed success/error union preserves that schema.
                return detailed  # pyright: ignore[reportReturnType]
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_history",
        description="List up to 32 retained encrypted correction records as metadata only, newest first, with can_undo for the latest unchanged correction. Retention is also capped at 1 MiB encrypted per document. No historical document text is returned; use review_undo dry_run for a bounded preview.",
    )
    async def document_review_history(
        document_id: str,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).history(
                    document_id,
                    expected_revision=expected_revision,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_document_review_undo",
        description="Undo the latest unchanged retained correction atomically, with a reviewed current revision, history change_id and stable retry key. dry_run previews without writing. Rejects stale or out-of-order undo; never rebases over later changes. Restores exact prior HTML and republishes Markdown/knowledge; metadata becomes stale, existing exports and Translator snapshots stay unchanged. No redo.",
    )
    async def document_review_undo(
        document_id: str,
        change_id: str,
        expected_revision: str,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.doc_processor")
        try:
            return await runtime.run(
                lambda: document_review(principal.owner_user_id).undo(
                    document_id,
                    change_id=change_id,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    dry_run=dry_run,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc
