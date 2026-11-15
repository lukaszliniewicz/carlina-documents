from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from flask import Flask
from pydantic import Field

from ...core.mcp_tool_runtime import McpToolRuntime
from .mcp_settings import EditReason, SentenceEdit

if TYPE_CHECKING:
    from mcp.server import MCPServer
    from mcp_types import CallToolResult

    from .api_client import TranslatorClient
    from .mcp_registration_types import SourcePageRenderer


def register_review_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    client: Callable[[Flask, int], TranslatorClient],
    source_pages: SourcePageRenderer,
    image_result: Callable[[dict[str, Any]], CallToolResult],
) -> None:
    @server.tool(
        name="carlina_translator_numbering_preview",
        description="Compare a translation's pinned source with its corrected source document. Returns exact-match list corrections, conflicts and a revision-bound preview_token. No text or numbering is changed. Review changes and select their IDs before applying; ambiguous lists require source review.",
    )
    async def translator_numbering_preview(project_id: str) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).numbering_preview(
                    project_id
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_numbering_apply",
        description="Apply explicitly selected list-numbering corrections from a fresh numbering preview. Preserves translated text, notes, alignments and pinned source. Requires its preview_token, selected change IDs and an idempotency key. Stale source/translation/overlay or active work is rejected. Regenerate exports afterward.",
    )
    async def translator_numbering_apply(
        project_id: str,
        preview_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        selected_ids: Annotated[list[str], Field(min_length=1, max_length=1000)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).numbering_apply(
                    project_id,
                    preview_token=preview_token,
                    selected_ids=selected_ids,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_numbering_undo",
        description="Undo the most recent numbering change for one translation, preserving its text. Get a fresh numbering preview first and supply its preview_token and an idempotency key. Cannot restore numbering across a changed source plan.",
    )
    async def translator_numbering_undo(
        project_id: str,
        preview_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).numbering_undo(
                    project_id,
                    preview_token=preview_token,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_review_outline",
        description=(
            "Read a paginated owner-scoped outline of verified document-block "
            "Translator evidence."
        ),
    )
    async def translator_review_outline(
        project_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).review_outline(
                    project_id, cursor=cursor, limit=limit
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_review_fetch",
        description=(
            "Fetch bounded verified document-block review evidence by block IDs or "
            "an inclusive order range. view=target_only returns the translation "
            "without source text, source headings, glossary or alignment, for the "
            "target-alone reading recorded as target_reading coverage; it cannot "
            "be combined with include_research or include_alignment."
        ),
    )
    async def translator_review_fetch(
        project_id: str,
        block_ids: list[str] | None = None,
        order_start: int | None = None,
        order_end: int | None = None,
        cursor: str | None = None,
        limit: int = 50,
        include_research: bool = False,
        include_proposals: bool = False,
        include_alignment: bool = False,
        view: Literal["bilingual", "target_only"] = "bilingual",
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).review_fetch(
                    project_id,
                    block_ids=block_ids,
                    order_start=order_start,
                    order_end=order_end,
                    cursor=cursor,
                    limit=limit,
                    include_research=include_research,
                    include_proposals=include_proposals,
                    include_alignment=include_alignment,
                    view=view,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_review_source_pages",
        description=(
            "Render the physical source PDF pages corresponding to 1-16 Translator block IDs. "
            "Translator resolves immutable source-span provenance first, then reuses Document Processor's "
            "bounded page renderer. At most four distinct physical pages may be returned per call."
        ),
    )
    async def translator_review_source_pages(
        project_id: str,
        block_ids: list[str],
        max_edge: int = 1600,
    ):
        principal = runtime.authorize("mcp.translator")
        try:
            resolution = await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).review_source_pages(project_id, block_ids=block_ids),
            )
            payload = await runtime.run(
                lambda: source_pages(
                    principal.owner_user_id,
                    resolution["document_id"],
                    resolution["page_numbers"],
                    max_edge=max_edge,
                ),
            )
            payload["translator_project_id"] = project_id
            payload["translator_blocks"] = resolution["blocks"]
            return image_result(payload)
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_qa_diagnostics",
        description=(
            "Run deterministic post-translation QA over active target revisions without changing targets "
            "or human review decisions. Detects structural evidence, missing/mutated/added citation keys, "
            "repeated punctuation, numbers present on one side only, unpaired brackets and "
            "quotation marks, script-aware source/target length anomalies including sentence "
            "groups much shorter than the rest of their block, glossary divergence including "
            "accepted contextual target variants, split/joined tokens, source-language leakage and spelling "
            "suspicions when an Enchant dictionary is available. include_spelling=false disables spelling "
            "advisories only. Filter by category/severity or block IDs, paginate with cursor, and inspect "
            "summary.truncated/omitted_* when a safety cap was reached. Findings dismissed through "
            "carlina_translator_qa_disposition_set are hidden by default (dispositions=open) and "
            "counted in summary.dismissed_total; dispositions=dismissed or all shows them with "
            "their reasons. A dismissal lapses when the target or the ruleset changes."
        ),
    )
    async def translator_qa_diagnostics(
        project_id: str,
        block_ids: list[str] | None = None,
        categories: list[str] | None = None,
        severities: list[str] | None = None,
        cursor: str | None = None,
        limit: int = 100,
        include_spelling: bool = True,
        group_by: Literal["finding", "token"] = "token",
        include_excerpts: bool = False,
        group_id: str | None = None,
        dispositions: Literal["open", "dismissed", "all"] = "open",
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).qa_diagnostics(
                    project_id,
                    block_ids=block_ids,
                    categories=categories,
                    severities=severities,
                    cursor=cursor,
                    limit=limit,
                    include_spelling=include_spelling,
                    group_by=group_by,
                    include_excerpts=include_excerpts,
                    group_id=group_id,
                    dispositions=dispositions,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_qa_disposition_set",
        description=(
            "Record a decision on one current QA finding. action=dismiss needs basis "
            "false_positive (the rule misfired) or intended (a deliberate deviation) and a "
            "one-line reason; action=reopen withdraws a dismissal. expected_sequence is the "
            "finding's disposition.sequence, or 0 when it has none. The finding must exist "
            "in a scan of the current target; fix real errors instead of dismissing them."
        ),
    )
    async def translator_qa_disposition_set(
        project_id: str,
        block_id: str,
        finding_id: str,
        action: Literal["dismiss", "reopen"],
        expected_sequence: int,
        idempotency_key: str,
        basis: Literal["false_positive", "intended"] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).qa_disposition_set(
                    project_id,
                    block_id=block_id,
                    finding_id=finding_id,
                    action=action,
                    expected_sequence=expected_sequence,
                    idempotency_key=idempotency_key,
                    basis=basis,
                    reason=reason,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_flag_create",
        description=(
            "Open a translator flag on one block: an assumption made, a suspected source "
            "defect, an unverified name, quotation or fact, a deliberate departure, or "
            "something to revisit. Flags are review records, never exported into the text; "
            "do not put doubts in the translation itself. excerpt may quote the span concerned."
        ),
    )
    async def translator_flag_create(
        project_id: str,
        block_id: str,
        kind: Literal[
            "assumption", "source_defect", "unverified", "departure", "revisit"
        ],
        body: Annotated[str, Field(min_length=1, max_length=1000)],
        idempotency_key: str,
        excerpt: Annotated[str, Field(max_length=300)] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).flag_create(
                    project_id,
                    block_id=block_id,
                    kind=kind,
                    body=body,
                    excerpt=excerpt,
                    origin="agent",
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_flag_update",
        description=(
            "Resolve, dismiss or reopen a translator flag. resolve and dismiss need a note "
            "saying what was decided; expected_sequence is the flag's current sequence."
        ),
    )
    async def translator_flag_update(
        project_id: str,
        flag_id: str,
        action: Literal["resolve", "dismiss", "reopen"],
        expected_sequence: int,
        idempotency_key: str,
        note: Annotated[str, Field(max_length=1000)] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).flag_update(
                    project_id,
                    flag_id=flag_id,
                    action=action,
                    expected_sequence=expected_sequence,
                    note=note,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_flag_list",
        description=(
            "List translator flags in document order, open ones by default, with counts "
            "by state and open counts by kind. Page with offset and limit (1-100)."
        ),
    )
    async def translator_flag_list(
        project_id: str,
        states: list[Literal["open", "resolved", "dismissed"]] | None = None,
        kinds: list[
            Literal["assumption", "source_defect", "unverified", "departure", "revisit"]
        ]
        | None = None,
        block_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).flag_list(
                    project_id,
                    states=states,
                    kinds=kinds,
                    block_ids=block_ids,
                    offset=offset,
                    limit=limit,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_review_coverage_record",
        description=(
            "Record revision-fenced structural, target-reading, or bilingual review coverage for 1-100 blocks. "
            "Each entry requires block_id, expected_target_version, scope=block or anchors, and source_anchor_ids. "
            "Use scope=block with an empty anchor list only after reviewing the entire block; otherwise list "
            "the reviewed source anchors. This records declared agent/human provenance and never approves prose. "
            "Supply a stable idempotency_key. Later target edits make coverage historical."
        ),
    )
    async def translator_review_coverage_record(
        project_id: str,
        entries: list[dict[str, Any]],
        kind: Literal["structural", "target_reading", "bilingual"],
        idempotency_key: str,
        reviewer_kind: Literal["agent", "human"] = "agent",
        comment: str = "",
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).review_coverage_record(
                    project_id,
                    entries=entries,
                    kind=kind,
                    idempotency_key=idempotency_key,
                    reviewer_kind=reviewer_kind,
                    comment=comment,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_review_coverage_get",
        description="Read current revision-bound review coverage and scan-wide counts, with at most 100 blocks per page. Anchor IDs are omitted by default. Structural checks and linguistic review are separate; coverage is declared evidence, not automatic approval.",
    )
    async def translator_review_coverage_get(
        project_id: str,
        block_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 100,
        include_anchor_ids: bool = False,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).review_coverage_get(
                    project_id,
                    block_ids=block_ids,
                    offset=offset,
                    limit=limit,
                    include_anchor_ids=include_anchor_ids,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_review_search",
        description=(
            "Search verified document-block evidence with bounded literal or regular-expression matching. "
            "Literal mode uses Unicode NFKC/casefold matching. Regex mode accepts only the explicit "
            "flags ignore_case, multiline, dotall and verbose and enforces both per-value and whole-search "
            "timeouts. Zero-width expressions are rejected."
        ),
    )
    async def translator_review_search(
        project_id: str,
        query: str,
        fields: list[str] | None = None,
        roles: list[str] | None = None,
        before: int = 0,
        after: int = 0,
        cursor: str | None = None,
        limit: int = 20,
        include_proposals: bool = False,
        mode: str = "literal",
        regex_flags: list[str] | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).review_search(
                    project_id,
                    query=query,
                    fields=fields,
                    roles=roles,
                    before=before,
                    after=after,
                    cursor=cursor,
                    limit=limit,
                    include_proposals=include_proposals,
                    mode=mode,
                    regex_flags=regex_flags,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_review_explain",
        description=(
            "Return bounded evidence for a question over selected blocks; this is "
            "not hidden reasoning or a model answer."
        ),
    )
    async def translator_review_explain(
        project_id: str,
        block_ids: list[str],
        question: str,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).review_explain(
                    project_id,
                    block_ids=block_ids,
                    question=question,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_export_metadata_prepare",
        description=(
            "Prepare bounded source metadata and excerpts for you to author translated-edition "
            "title, author credits, description and subjects. No provider call or saved-data "
            "change. Follow editable_fields and response_schema, merge the authored fields "
            "with returned metadata, then use export_metadata_update with expected_version. "
            "Preserve user credits and do not invent publication facts."
        ),
    )
    async def translator_export_metadata_prepare(project_id: str) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).export_metadata_prepare(project_id),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_target_update",
        description=(
            "Append and activate one owner-authored document-block target correction "
            "using an explicit active-version fence and idempotency key. Local wording "
            "edits retain existing sentence groups when their boundaries survive. "
            "For an explicit sentence operation, omit candidate and supply sentence_edit: "
            "replace keeps boundaries, split uses a Unicode character offset, and "
            "merge_next joins adjacent target segments. Source segmentation stays fixed. "
            "Set restore_parent_alignment=true only to recover the immediate parent "
            "revision's pairing; unsafe or ambiguous recovery is rejected. reason names "
            "the defect the change removes (meaning, omission, addition, language, "
            "terminology, consistency, brief, misreading, formatting) with one line on "
            "what was wrong; it is stored with the revision and shown in review. A "
            "preferred wording is not a defect: leave the text."
        ),
    )
    async def translator_target_update(
        project_id: str,
        block_id: str,
        expected_active_version: int,
        idempotency_key: str,
        reason: EditReason,
        candidate: dict[str, Any] | None = None,
        restore_parent_alignment: bool = False,
        sentence_edit: SentenceEdit | None = None,
        response_mode: Literal["compact", "full"] = "compact",
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).target_update(
                    project_id,
                    block_id,
                    candidate,
                    expected_active_version,
                    idempotency_key,
                    restore_parent_alignment=restore_parent_alignment,
                    response_mode=response_mode,
                    edit_reason=reason.model_dump(),
                    **(
                        {"sentence_edit": sentence_edit.model_dump()}
                        if sentence_edit is not None
                        else {}
                    ),
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_reader_note_create",
        description=(
            "Create an owner-scoped reader note on one verified document block."
        ),
    )
    async def translator_reader_note_create(
        project_id: str,
        block_id: str,
        body: str,
        anchor: dict[str, Any],
        idempotency_key: str,
        citation_ids: list[str] | None = None,
        state: str = "active",
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).reader_note_create(
                    project_id,
                    block_id=block_id,
                    body=body,
                    anchor=anchor,
                    idempotency_key=idempotency_key,
                    citation_ids=citation_ids,
                    state=state,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_reader_note_revise",
        description=(
            "Revise an owner-scoped reader note using an explicit version fence."
        ),
    )
    async def translator_reader_note_revise(
        project_id: str,
        note_id: str,
        body: str,
        expected_version: int,
        idempotency_key: str,
        anchor: dict[str, Any] | None = None,
        citation_ids: list[str] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).reader_note_revise(
                    project_id,
                    note_id=note_id,
                    body=body,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    anchor=anchor,
                    citation_ids=citation_ids,
                    state=state,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_reader_note_set_state",
        description=(
            "Set an owner-scoped reader note state using an explicit version fence."
        ),
    )
    async def translator_reader_note_set_state(
        project_id: str,
        note_id: str,
        state: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).reader_note_set_state(
                    project_id,
                    note_id=note_id,
                    state=state,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc
