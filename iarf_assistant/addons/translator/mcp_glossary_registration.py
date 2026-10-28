from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal

from flask import Flask
from pydantic import BaseModel, ConfigDict, Field

from ...core.mcp_tool_runtime import McpToolRuntime
from .glossary_contract import GlossaryTermInput

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .api_client import TranslatorClient


class SharedGlossaryDecision(BaseModel):
    """One explicit choice for a row in a pinned shared glossary preview."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=128)
    choice: Literal["incoming", "local"]


SharedGlossaryId = Annotated[str, Field(strict=True, min_length=1, max_length=36)]
SharedGlossaryConceptId = Annotated[
    str, Field(strict=True, min_length=1, max_length=128)
]
SharedGlossaryConceptIds = Annotated[
    list[SharedGlossaryConceptId], Field(max_length=2000)
]
SharedGlossaryRetryKey = Annotated[
    str, Field(strict=True, min_length=1, max_length=160)
]
SharedGlossaryName = Annotated[str, Field(strict=True, min_length=1, max_length=255)]


def register_glossary_tools(
    server: MCPServer,
    runtime: McpToolRuntime,
    *,
    client: Callable[[Flask, int], TranslatorClient],
) -> None:
    @server.tool(
        name="carlina_translator_glossary_get",
        description=(
            "Read/search current project terminology with revision fences, senses, "
            "observed source forms and exact source anchors. Paginated with an honest "
            "byte budget (tail entries move to next_offset, never silently dropped); "
            "pass include_observations=false for a bounded compact projection. Query matches "
            "headword, form, sense or target. Model entries are immediately usable advice; "
            "human locks remain authoritative. Research dossiers are separate; routine "
            "glossary writes are not dossiers and never imply human approval."
        ),
    )
    async def translator_glossary_get(
        project_id: str,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
        include_observations: bool = True,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).glossary_get(
                    project_id,
                    query=query,
                    offset=offset,
                    limit=limit,
                    include_observations=include_observations,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_glossary_propose",
        description=(
            "Add or revise 1-32 live model glossary concepts atomically, including during "
            "translation. Use canonical citation forms, a sense where ambiguous, and preferred "
            "target wording with grammatical inflection; exact is for fixed strings only. "
            "Human locks are authority, not exact-character invariants: preferred targets "
            "inflect. accepted_target_forms stores deliberate contextual target renderings; "
            "omit it on edits to retain prior variants or supply the full replacement list. "
            "For edits read first and supply concept_id plus expected_revision; lexical fields "
            "replace, omitted source_forms retain prior forms. Exact quotes "
            "plus block IDs let Carlina derive codepoint offsets; repeated quotes require "
            "a one-based occurrence_index. Newly prepared packets carry a read-only "
            "translator_window_index (local n to canonical block_id plus sentence anchor "
            "ids) in dispatch metadata; use it instead of extra outline fetches. New forms "
            "are model associations, not certified morphology. Stable idempotency_key retries "
            "reuse receipts. Cannot overwrite human locks. Propose BEFORE submitting the "
            "current window translation when the next window should see it; already "
            "prepared or leased packets and frozen research do not change. No dossier needed; "
            "do not disguise routine writes as dossiers or human approval."
        ),
    )
    async def translator_glossary_propose(
        project_id: str,
        source_plan_id: str,
        entries: Annotated[list[GlossaryTermInput], Field(min_length=1, max_length=32)],
        idempotency_key: str,
        provenance: str = "passive-executor",
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(runtime.app, principal.owner_user_id).glossary_propose(
                    project_id,
                    source_plan_id=source_plan_id,
                    entries=[entry.model_dump() for entry in entries],
                    idempotency_key=idempotency_key,
                    provenance=provenance,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_list",
        description=(
            "List your owner-scoped shared Translator glossary assets. Use the returned asset ID "
            "with shared_glossary_get to inspect an exact immutable revision and pending proposals. "
            "Shared terms remain advisory; publication is not human approval."
        ),
    )
    async def translator_shared_glossary_list(
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_list(offset=offset, limit=limit),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_get",
        description=(
            "Read one owned shared glossary revision and paginated pending proposals. Pin the exact "
            "revision you intend to use; do not assume the latest revision remains the one a project "
            "has adopted. Proposal changes remain advisory until separately reviewed."
        ),
    )
    async def translator_shared_glossary_get(
        asset_id: SharedGlossaryId,
        revision: Annotated[int, Field(strict=True, ge=1)] | None = None,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
        proposal_offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        proposal_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 10,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_get(
                    asset_id,
                    revision=revision,
                    offset=offset,
                    limit=limit,
                    proposal_offset=proposal_offset,
                    proposal_limit=proposal_limit,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_create",
        description=(
            "Create an owner-scoped immutable shared asset from selected current concept IDs in an "
            "owned project. Get those IDs from the project glossary first. Creation publishes only "
            "advisory terms; it does not create human approval, import them elsewhere, or rewrite targets. "
            "Use a stable idempotency_key for retries."
        ),
    )
    async def translator_shared_glossary_create(
        project_id: SharedGlossaryId,
        name: SharedGlossaryName,
        concept_ids: SharedGlossaryConceptIds,
        idempotency_key: SharedGlossaryRetryKey,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_create(
                    project_id,
                    name=name,
                    concept_ids=concept_ids,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_project",
        description=(
            "Read an owned project's local terms, source-plan fence, shared binding and binding version. "
            "Use this before preview, detach, or a contribution so your IDs and expected versions are current. "
            "Imported advice applies to a later operation only."
        ),
    )
    async def translator_shared_glossary_project(
        project_id: SharedGlossaryId,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_project(project_id, offset=offset, limit=limit),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_preview",
        description=(
            "Preview an exact immutable shared asset revision against the current project glossary. "
            "Choose mode explicitly: link pins the asset for later reviewed updates; update adopts a "
            "selected revision of the already linked asset; copy selects independent terms and retains "
            "any existing link. Preview fences the source plan, asset revision, "
            "local glossary and binding; it is read-only and can be prepared while a project is running. "
            "Review every row and pass the returned preview_token to apply."
        ),
    )
    async def translator_shared_glossary_preview(
        project_id: SharedGlossaryId,
        asset_id: SharedGlossaryId,
        revision: Annotated[int, Field(strict=True, ge=1)],
        mode: Literal["link", "copy", "update"],
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_preview(
                    project_id,
                    asset_id=asset_id,
                    revision=revision,
                    mode=mode,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_apply",
        description=(
            "Apply one previewed shared revision. Link/update require incoming or local decisions for every "
            "changed row; copy accepts a selected incoming subset without creating or replacing a link. "
            "Use the exact preview_token and revision, and a stable idempotency_key. Link, "
            "copy, and update require an idle project boundary. Applying creates/links model-advisory terms; "
            "it never rewrites target text, creates human approval, changes acquired packets, or silently "
            "synchronizes later revisions. Human-locked local terms cannot be overwritten."
        ),
    )
    async def translator_shared_glossary_apply(
        project_id: SharedGlossaryId,
        asset_id: SharedGlossaryId,
        revision: Annotated[int, Field(strict=True, ge=1)],
        mode: Literal["link", "copy", "update"],
        preview_token: Annotated[
            str,
            Field(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
        ],
        decisions: Annotated[list[SharedGlossaryDecision], Field(max_length=2000)],
        idempotency_key: SharedGlossaryRetryKey,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_apply(
                    project_id,
                    asset_id=asset_id,
                    revision=revision,
                    mode=mode,
                    preview_token=preview_token,
                    decisions=[decision.model_dump() for decision in decisions],
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_detach",
        description=(
            "Detach the project's current shared asset at an idle boundary. Read the current binding "
            "version first and supply it as expected_version with a stable idempotency_key. Detaching "
            "does not delete local terminology or rewrite targets."
        ),
    )
    async def translator_shared_glossary_detach(
        project_id: SharedGlossaryId,
        expected_version: Annotated[int, Field(strict=True, ge=1)],
        idempotency_key: SharedGlossaryRetryKey,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_detach(
                    project_id,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_contribute",
        description=(
            "Propose selected current local concept changes and/or shared term retirements against an "
            "expected asset revision. This creates a pending advisory proposal only; it neither accepts "
            "the change nor locks local terms. Contributions may be prepared while translation runs, "
            "but acquired packets stay unchanged. Use a stable idempotency_key."
        ),
    )
    async def translator_shared_glossary_contribute(
        project_id: SharedGlossaryId,
        asset_id: SharedGlossaryId,
        expected_revision: Annotated[int, Field(strict=True, ge=1)],
        concept_ids: SharedGlossaryConceptIds,
        reason: Annotated[str, Field(strict=True, min_length=1, max_length=8192)],
        idempotency_key: SharedGlossaryRetryKey,
        retire_ids: SharedGlossaryConceptIds | None = None,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_contribute(
                    project_id,
                    asset_id=asset_id,
                    expected_revision=expected_revision,
                    concept_ids=concept_ids,
                    retire_ids=retire_ids,
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc

    @server.tool(
        name="carlina_translator_shared_glossary_review",
        description=(
            "Review one pending proposal owned by this connection after reading it with "
            "shared_glossary_get. Pass the current expected asset revision and accept or reject explicitly. "
            "Acceptance creates a new immutable advisory asset revision; it creates no human locks and "
            "does not sync other projects. Each project must preview and adopt it at its own idle boundary. "
            "Use a stable idempotency_key."
        ),
    )
    async def translator_shared_glossary_review(
        asset_id: SharedGlossaryId,
        proposal_id: SharedGlossaryId,
        expected_revision: Annotated[int, Field(strict=True, ge=1)],
        decision: Literal["accept", "reject"],
        idempotency_key: SharedGlossaryRetryKey,
    ) -> dict[str, Any]:
        principal = runtime.authorize("mcp.translator")
        try:
            return await runtime.run(
                lambda: client(
                    runtime.app, principal.owner_user_id
                ).shared_glossary_review(
                    asset_id,
                    proposal_id=proposal_id,
                    expected_revision=expected_revision,
                    decision=decision,
                    idempotency_key=idempotency_key,
                ),
            )
        except BaseException as exc:
            raise runtime.failure(exc) from exc
