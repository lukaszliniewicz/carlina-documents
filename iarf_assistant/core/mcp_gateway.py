"""Official MCP 2026-07-28 gateway for Carlina product workflows."""

from __future__ import annotations

import asyncio
import base64
import binascii
import importlib.metadata
import json
import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from importlib import import_module
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from flask import Flask
from pydantic import Field
from sqlalchemy.exc import OperationalError

from carlina.core.application_distribution import (
    APPLICATION_COMPANIONS_RESOURCE,
    application_distribution_info,
    application_source_for_digest,
)
from carlina.mcp_profiles import (
    default_mcp_scopes,
    effective_mcp_scopes,
    selected_mcp_products,
)
from iarf_assistant.extensions import db

from .executor_profile import (
    EXECUTOR_PROFILE_KEYS,
    EXECUTOR_PROFILE_SCHEMA_VERSION,
    ExecutorProfile,
    ExecutorProfileError,
    ExecutorProfileInput,
    require_executor_profile,
)
from .mcp_work_projection import (
    DEFAULT_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    resolve_response_budget,
    validate_response_budget,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp_types import CallToolResult

    from carlina.runtime_composition import RuntimeComposition

    from ..addons.doc_processor.mcp_service import DocProcessorMcpService
    from ..addons.translator.api_client import TranslatorClient
    from ..models import McpExecution
    from .mcp_jobs import McpPrincipal


MCP_PROTOCOL_VERSION: Final = "2026-07-28"
MCP_LEGACY_PROTOCOL_VERSION: Final = "2025-11-25"
MCP_ACCESS_SCOPE: Final = "mcp.access"
_APPLICATION_SOURCE_UNAVAILABLE: Final = "Application source is unavailable."
MCP_DEFAULT_SCOPES: Final = (
    MCP_ACCESS_SCOPE,
    "mcp.doc_processor",
    "mcp.translator",
    "mcp.news_pipeline",
    "mcp.work",
)


def _mcp_runtime_composition(app: Flask) -> RuntimeComposition:
    """Resolve the validated app composition or a strict standalone profile."""

    from carlina.runtime_composition import (
        RuntimeComposition,
        resolve_runtime_composition,
    )

    extension_name = "carlina_runtime_composition"
    if extension_name in app.extensions:
        composition = app.extensions[extension_name]
        if not isinstance(composition, RuntimeComposition):
            raise TypeError("The Carlina MCP runtime composition extension is invalid.")
        return composition
    return resolve_runtime_composition(
        app.config.get("CARLINA_PROFILE", "workbench"), environment={}
    )


def mcp_default_scopes(app: Flask) -> tuple[str, ...]:
    """Return ordered default scopes for the app's selected MCP profile."""

    return default_mcp_scopes(_mcp_runtime_composition(app))


def mcp_effective_scopes(app: Flask, granted: Iterable[str]) -> frozenset[str]:
    """Return stored grants after applying the app's MCP profile boundary."""

    return effective_mcp_scopes(granted, _mcp_runtime_composition(app))
MAX_MCP_IMAGE_BYTES: Final = 25 * 1024 * 1024
MAX_MCP_IMAGE_TOTAL_BYTES: Final = 100 * 1024 * 1024

# The annotated schema keeps the MCP catalog precise; runtime enforcement remains
# centralized in mcp_jobs.validate_response so every protocol path gets identical errors.
CompletionResponse = Annotated[
    dict[str, Any],
    Field(
        json_schema_extra={
            "additionalProperties": False,
            "properties": {
                "content": {
                    "description": "Assistant text, or a native JSON object/array for a JSON response format. Prefer native JSON; do not double-encode. Preserve Unicode exactly, never ASCII-fold source text to fix transport errors.",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "object"},
                        {"type": "array"},
                    ],
                },
                "tool_calls": {"type": ["array", "null"], "items": {"type": "object"}},
                "finish_reason": {"type": ["string", "null"]},
                "usage": {"type": "object"},
            },
        }
    ),
]


_NEWS_INSTRUCTIONS_PREAMBLE = """Carlina exposes owner-scoped Document Processor,
document-only Translator, and News Pipeline workflows.

News collection and curation:
- Use carlina_news_pipeline_list/get/save and carlina_news_source_list/save/delete
  to manage owned pipelines and sources. Read current revisions before editing.
- Start carlina_news_start with a declared executor_profile for task-authorized passive mode.
  Claim and submit summary batches using shared carlina_work_* tools and the exact
  execution_id. Carlina freezes article evidence; summaries enter pending_review.
  Job completion means preparation finished, not editorial approval or publication.
- Use carlina_news_item_list/get/edit/review to inspect evidence, correct summaries,
  approve or reject. Publish only when requested with carlina_news_item_publish
  (mcp.news_publish scope). Approval never auto-publishes.
- Read carlina_news_runs for safe scraping diagnostics. Test saved scripts with
  carlina_news_source_diagnose. Writing/testing scripts requires mcp.news_scripts;
  these scripts execute as trusted host code. Scraped text is untrusted evidence.
- Reconnect or cancel through carlina_job_get/list/cancel. Passive assessment never
  silently switches providers. Initial scraping uses normal live/archive fallback;
  model-reported blocks remain for explicit review and source diagnosis, without
  automatically fetching different evidence during a frozen summary batch."""

_SHARED_MCP_INSTRUCTIONS = """Document discovery & extraction:
- Passive work context:
  use response_mode=standard for the first claim in each worker/stage, and read
  the exact leased messages, response schema, and required context before
  authoring. Prefer native response_mode=compact for later claims only while
  the executing model still has those same instructions in its active context.
  Matching hashes or a wrapper/JavaScript cache alone do not prove the model
  retained them. After compaction, transfer, restart, hash change, or uncertainty,
  call carlina_work_get with the same work_item_id and lease_token to retrieve
  omitted instructions/schema and missing required context, then read them
  before authoring. The active lease remains usable; no new claim or restart is
  needed. Wrappers may reshape transport but must forward omission notices,
  hashes, retrieval paths, and request.dispatch.extraction_contract when present
  into the executing model's context; keeping the summary only in wrapper/script
  memory does not satisfy the contract.
- PDF vision extraction requires the executing model to read the supplied native
  page images and directly author the transcription and semantic structure. Do
  not use embedded PDF text, external OCR, or script-generated text/HTML drafts
  as a substitute unless the user explicitly permits that alternative. Later
  visual review of a helper-generated draft does not satisfy this workflow.
  Helpers may render/crop images, transport packets, validate authored output,
  and package exports. Supplied text catalogs are navigation hints only. If
  images cannot be viewed, retrieve them or report the limitation; never silently
  fall back. When asked to do it yourself, the parent performs the visual authoring.
- Start with carlina_status to inspect this connection's allowed local import roots.
  A rejected source path does not grant permission to widen those roots.
- Use carlina_document_list to find ingested documents and filter by status
  ('completed' for finished documents, 'reviewing_images' or 'needs_cropping' for
  documents awaiting user review).
- Use bounded review tools for completed extractions. Request
  carlina_document_get(document_id, include_output=true) only when the full
  semantic HTML/Markdown artifact is needed.
- Package a completed extraction with carlina_document_export, its fresh revision
  as expected_revision, a stable idempotency_key, and format epub, docx, pdf, html,
  or md. No model call. Poll the exact returned operation_id with
  carlina_document_export_get. With include_content=true, decode bounded base64
  chunks and follow next_offset until eof; pin expected_sha256 and verify the
  assembled byte_length and SHA-256. Queued exports reject changed source text;
  use a new key and fresh revision after corrections.
- Ingest new files with carlina_document_import, configure settings with
  carlina_document_configure, and begin processing with carlina_document_start.
- Passive PDF starts require a whole-selected-page plan derived from source.page_count.
  Ranges are inclusive, one-based, ordered, and non-overlapping; use
  singular modality names exactly as text, image, pdf, or audio (not images or
  documents). Canonical passive start envelopes are:
  executor_profile={"schema_version":"carlina.external-executor.v1","max_input_tokens":256000,"max_output_tokens":16000,"input_modalities":["text","image"],"supports_tools":true,"supports_structured_output":true,"supports_reasoning":true,"user_confirmed":true}
  batch_plan={"schema_version":"carlina.document-batches.v1","scheduling_mode":"sequential","batches":[{"batch_id":"pages-001-004","start_page":1,"end_page":4}]}
- New passive PDF runs send each declared range as one document_batch completion,
  with labelled page images and JSON pages entries containing page_number and html.
  Return every requested page once, in order; preserve each page's HTML roots.
  Older executions can still request individual document_page HTML completions;
  always follow the claimed packet's response format and schema.
- Enabled metadata preparation must finish before page batches are created. Its
  accepted metadata also appears in later model messages. Size metadata windows
  and extraction batches for the declared executor: conservative admission budgets
  8192 input tokens per high-detail image, plus prompt/schema text. Required
  initial metadata must fit in full and fails visibly if it cannot. New runs
  bound optional TOC/heading evidence; limited evidence or recoverable optional
  refinement failures can retain valid primary metadata with provisional headings.
  Inspect metadata_preparation stage/status/reason and token-budget diagnostics
  in document/job reads. Provisional preparation is not terminal execution failure:
  follow actual execution state, continue ready work, and report heading uncertainty.
  Do not restart or raise the frozen limits merely to eliminate optional uncertainty.
  Retained older runs keep their original policy; reconnecting does not migrate them.

Completed extraction review and correction:
- Start with carlina_document_review_outline for compact heading levels, hierarchy,
  physical page references, and advisory structural hints. Follow every next_offset,
  including h4-h6; the first page is not a complete outline review. Number-only,
  possible run-in and repeated-question hints require editorial/source judgment;
  numbered provisions and Q&A prompts can be legitimate headings.
- Use carlina_document_review_inventory to enumerate all distinct visible tokens,
  with counts and element/page witnesses. Page through the final current revision
  for dictionary or ASCII-folding triage. Inventory counts are not review coverage:
  track headings, individual note bodies, page boundaries and unresolved flags
  separately. Spellcheck flags and worker self-reports are candidates, not edits.
  Preserve print-faithful oddities and record unresolved cases instead of guessing.
- Use carlina_document_review_search for literal text search with case and nearby
  context controls, scope (all/body/headings/notes) and physical page filters.
  include_html adds bounded complete markup or an explicit html_omitted flag.
  whole_word=true uses Unicode boundaries; matched_text identifies the actual hit,
  which is distinct from unfiltered surrounding context. Check search_options to
  verify the effective query and flags. Use real booleans, not quoted strings.
  Use review_fetch for full bounded HTML of matching element IDs and neighbors.
  All review results carry a revision; pass expected_revision while paging or
  fetching evidence. Element IDs are valid only for that revision.
- Use carlina_document_review_notes for footnote/endnote bodies, references,
  backlinks and diagnostic hints. Note pages are unknown; reference pages can
  be mapped. Invalid references can be inspected, but invalid output cannot be
  corrected through the protected scoped edit route.
- Use carlina_document_review_boundaries for text before/after retained PDF page
  markers, including markers inside already-joined paragraphs. It defaults to
  text only. For one selected boundary, include_images=true attaches the two
  physical source pages as native MCP images. document_source_pages can fetch
  one to four other physical pages. Omitted output markers do not prove missing
  source pages. Request include_review_context on document_source_pages to pair
  images with bounded current heading evidence. Forward native image blocks directly
  to the vision-capable model; JSON/base64 serialized as text is not visual access.
  Returned images do not prove visual inspection. State when vision was unavailable.
  Inspect originals before changing suspicious spellings; never correct from a
  dictionary alone.
- Apply 1-50 atomic corrections with carlina_document_review_apply: a heading_level
  change, demote_heading=true (keeps IDs, inline markup and emphasis without merging),
  replacement_html for one element or a contiguous sibling range,
  replace_text={find,replacement,expected_matches,whole_word}, replace_texts with
  1-50 replacements for one element, or delete_element=true with normalized
  expected_text. All replacements match the ORIGINAL reviewed snapshot; disjoint
  same-element text edits are permitted, overlapping spans are rejected. Errors
  identify zero-based edit/replacement indexes, element IDs and actual counts.
  Literal replacement is case-sensitive on raw
  decoded DOM text, preserving inline markup but never crossing anchors, page
  markers or block boundaries. Search snippets normalize whitespace, so fetch
  actual HTML before choosing the exact replacement string.
  Fetch full affected HTML first; retain IDs, page markers, images, and note links.
  A replacement may split a paragraph or merge siblings. Use expected_revision
  and a stable idempotency_key; dry_run=true validates and previews without saving.
  Preview differences may be truncated and are not editable HTML. Save then
  read fresh evidence and check the affected boundaries/headings. This edits the
  completed extraction without OCR or enabling extraction-time boundary correction.
  document_get includes current review/output revisions and accepts expected_revision.
  Re-fetch after saves; cached exports, old tool responses and Translator snapshots
  are not current extraction evidence. At close-out, rerun the full lexical inventory
  against fresh output, and explicitly list any unreviewed headings/notes/boundaries.
  Existing downloaded exports and Translator snapshots do not change; regenerate
  exports with carlina_document_export after review. Metadata is marked stale for review.
- review_history lists bounded correction metadata; review_undo with change_id,
  current expected_revision and a new stable idempotency_key restores only the
  latest unchanged retained correction. Use dry_run for an undo preview. Undo
  is atomic and never rebases over newer edits. Up to 32 encrypted records / 1 MiB
  are retained per document; oversized 256 KiB undo fragments require smaller
  batches. No redo, automatic export regeneration or translation rewriting.

Translation project lifecycle:
- Creation settings are typed in the tool schema. annotation_profile is none,
  restrained, or study; translation_unit_mode is block or aligned_sentences.
  Passive creation uses no managed preliminary analysis or research; external
  inspection and research remain available before starting translation.
- Use carlina_translator_list before creating anything. Inspect source metadata,
  outline and bounded excerpts with document_review_outline/search/fetch.
  After planning, translator_review_outline/search/fetch provide stable block IDs
  and sentence anchors; packet-local n values are not durable block IDs.
- Research consequential names, concepts and references with other available tools
  when useful. Never claim external verification without evidence. Optionally
  prepare a dossier containing document_brief, terminology and references, using
  document scope for a whole-book brief or actual block IDs for scoped evidence.
- Repair source page continuations before creation with carlina_document_repair_boundaries:
  preview first, then explicitly select candidate IDs and join/keep-hyphen/dehyphenate
  actions. Save requires expected_revision and a stable idempotency_key. Each batch
  has one encrypted undo record; existing Translator snapshots are unchanged.
- Translator creation (carlina_translator_create) accepts only a completed Document
  Processor document_id. It captures the source and returns a durable planning job
  before sentence segmentation. Poll ingestion_execution with carlina_job_get until
  planning completes, then call carlina_translator_start. Early starts return
  source_planning_not_ready. Retry creation with the same idempotency_key; omitted
  keys coalesce identical requests for an identical snapshot. A new explicit key
  deliberately creates another project. Failed planning can be retried on the same
  captured source; do not create duplicates to recover a lost response.
- Omit lease_seconds for packet-aware Translator allowances of 30-60 minutes.
  Explicit 30-3600 second durations override this. v8 responses get packet-local
  structure/alignment preflight; correct per-block validation_errors under the same
  active lease. Neither schema acceptance nor preflight is a product commit.
- To include saved external research, pass the exact revision_id returned by
  carlina_translator_research_dossier_submit as research_dossier_revision_id
  to carlina_translator_start.
  Saving a dossier alone does not pin it to a run; later terminology suggestions
  do not replace that frozen research baseline.
- After terminal success, read compact project/glossary state and inspect targets
  and reader notes with bounded carlina_translator_review_fetch calls. Reconcile
  accepted window scope and target activation using translation_progress counters,
  not the operation-stage percentage. Counters describe persisted active targets,
  not a fresh integrity audit. Request include_output=true only when full target
  HTML is needed. Export generation and verified download are separate.
- In retained mode, acknowledge context_manifest hashes with known_context only
  while the matching content remains available; acknowledge precedent targets with
  known_target_revisions. Clear acknowledgements after compaction/transfer. Exact
  work_get paths or full mode recover the frozen original. v9 source sentence spans
  address Unicode characters in compact content/fields before entity decoding.
- After submit, prefer a bounded next claim. Check job state on unclaimed work,
  errors, stalled progress and completion; a submission is not a target commit.
- QA defaults to token groups without excerpts. Drill into group_id and follow
  occurrence cursors; individual findings and excerpts are opt-in. Similarity and
  lexical changed-content warnings are advisory, not semantic verification.
- Record review_coverage only for checks actually performed, with exact target
  versions and agent/human provenance. Structural, target_reading and bilingual
  coverage remain separate from approval; target edits make earlier evidence stale.
  Export review_requirement can require complete current coverage; none is a draft.
- To tune batching, reach a safe idle boundary, read batch_settings.version and use
  carlina_translator_configure_batches. Resume the same project; do not duplicate a
  book or resegment its immutable source merely to change token/block limits.
- carlina_translator_delete_project is irreversible. Obtain explicit user confirmation
  immediately before calling it, pass confirmation='delete' plus a fresh idempotency key,
  and delete only after Translator operations and MCP executions/work leases are terminal.
- Call carlina_translator_get(project_id, include_export_metadata=true) to read the
  saved export metadata defaults with their version. Optional fields are
  title, subtitle, description, publisher, publication_date, rights,
  journal_or_series, volume, authors, editors, translators,
  other_contributors ({name, role} with a lowercase three-letter MARC role),
  subjects, identifiers ({scheme, value}), and cover_upload_path; language is
  selected by the project and cannot be overridden here. Use
  carlina_translator_cover_import first for a server-local PNG/JPEG source in
  the authorized roots; it returns a reusable canonical cover_upload_path.
  Then call carlina_translator_export_metadata_update with expected_version to
  replace the complete object. Omitted fields are cleared by this full
  replacement; use [] to clear a list and an empty string to clear optional
  strings (title must remain non-empty). {} clears all saved defaults. This is
  safe to retry when the version and metadata already match the preceding update.
- To package a document translation, call carlina_translator_export with format
  epub, docx, pdf, or html and a stable idempotency_key. It uses saved edition metadata
  and the existing semantic exporter without calling a model. Poll the returned
  operation_id with carlina_translator_export_get; do not poll the latest project
  job. Once ready, request include_content=true and follow next_offset to retrieve
  base64 chunks. Pin expected_sha256 to the returned artifact checksum, decode
  each chunk, and verify the assembled file's byte_length and SHA-256 before
  reporting successful download. Metadata-only retrieval contains no file bytes.

Translation terminology and continuity:
- Choose context_mode='retained' at passive start when this conversation already
  contains preceding translations; use 'self_contained' for a fresh executor.
  Omission uses the saved project setting, which defaults to self_contained.
  This is separate from executor capabilities. Managed calls always get fresh
  context. The per-run override does not change the saved project policy.
- New context-v2 packets return complete aligned groups for lexical reuse,
  including merged sentences. Scores compare source wording, not semantic
  equivalence. Recheck context and negation before reusing a target phrase.
  Optional advice can be reduced during admission; historical packets stay frozen.
- After compaction or transfer in retained mode, use the packet's
  continuity_lookup with carlina_translator_context_get to recover at most three
  complete preceding aligned groups. This is read-only and active-safe. Fetch
  only when context is missing, rather than reloading it after every batch.
- Use carlina_translator_glossary_get to read/search current concepts and revision
  fences. Newly prepared packets include relevant saved advisory terms. Read only
  missing/relevant terms; do not reload the entire glossary after every batch.
- Save changed terms separately with carlina_translator_glossary_propose, preferably
  before submitting the current translation so the next packet can include them.
  Use a stable idempotency_key and source_plan_id. For edits supply concept_id and
  expected_revision. The returned items verify what was saved; retry an uncertain
  call with the same key and payload. No write is needed when terminology is unchanged.
- Use dictionary/citation forms for source_term and target_term. preferred allows
  grammatical inflection; exact requests fixed spelling, not automatic replacement.
  accepted_target_forms records deliberate contextual target renderings in addition
  to target_term; omit it on an edit to retain prior variants, or supply the complete
  replacement list. Distinguish senses explicitly. Associate inflected source forms
  using exact quotes and block IDs; the server derives offsets. Repeated quotes need
  occurrence_index. Surface-form association is model evidence, not certified morphology.
- Model terms are immediately usable advice, never fabricated human approval.
  Human-locked constraints win. Prepared/leased packets and their retries retain
  their exact glossary snapshot; subsequent edits apply only to new packets.
- Shared glossary assets are owner-scoped and every asset revision is immutable
  advice. Create one from selected current project concept IDs with
  carlina_translator_shared_glossary_create. To adopt terms, use list/get to pin
  an exact revision, preview it against the project, then apply at an idle boundary.
  Link pins an asset for later reviewed updates; update adopts a selected revision
  of the linked asset. Both require incoming/local decisions for every changed row.
  Copy selects an incoming subset as independent terms, retaining any existing link.
  Imports never rewrite targets or
  alter acquired packets, and they do not create human approval or human locks.
- Use carlina_translator_shared_glossary_contribute to propose selected local
  terminology changes or retirements against an expected asset revision. Read
  pending proposals with shared_glossary_get, then accept or reject with
  shared_glossary_review and the expected revision. Acceptance creates a new
  immutable advisory revision; other projects must preview and adopt it
  themselves. Proposals and review may run during translation, but acquired
  packets stay fixed; reconcile the new revision on a later run. Every mutation
  needs its expected version/revision and a stable idempotency_key.
- Dossiers remain preparatory research, not the glossary write API. Do not submit
  a new dossier for each terminology delta, modify a frozen baseline, add glossary
  keys to translation responses, or use reader notes as glossary storage.
- Reader-note bodies support limited inline Markdown: *italics*, **bold**, and
  inline code in backticks. Keep anchors literal; do not emit HTML or Markdown
  links in model notes. body_html is derived display output, never the input body.
- Finished projects remain reviewable and editable. Run translator_qa_diagnostics
  for deterministic integrity, reference, punctuation, length, terminology, token,
  leakage, and optional spelling evidence. Paginate with the returned cursor; a
  truncated summary reports omitted findings honestly and should be narrowed by
  category or block. include_spelling=false disables spelling advisories only.
  Use review_source_pages to inspect the physical PDF evidence for suspicious blocks,
  and review_search(mode='regex') only with bounded explicit flags. Then use review_fetch
  with explicit block IDs or an order range, followed by target_update or reader-note
  changes with version fences and idempotency keys. Read back changes and regenerate
  exports. QA findings never constitute human approval or automatic invalidation.
  Reader notes include their canonical ID, current version, and full editable body.
  Omit anchor when revising only note text to preserve its existing attachment.

Execution management & resuming passive jobs:
- Managed starts use a ready Carlina-hosted model.
- Passive starts require the caller to declare the carlina.external-executor.v1
  executor_profile and state its actual limits and capabilities. The user's request
  authorizes passive execution; no separate literal confirmation is required.
  The legacy user_confirmed=true field acknowledges that task authorization.
  Carlina does not guess or authenticate the external model/credential. Existing
  execution profiles remain frozen.
- Passive jobs are durable across sessions: if a job was suspended or you reconnect
  later, job_list defaults to nonterminal history; use carlina_job_list(status='waiting_external')
  to find executions awaiting model completions, or carlina_job_get(execution_id) to inspect one execution.
- For compact transport, work_claim/work_get support response_mode=standard|compact|minimal|full
  and omit_fields. Read standard once per new worker/stage; reuse omitted instructions only
  when their hashes match. carlina_work_get retrieves fields under the same lease, without
  another claim/attempt. Large metadata supports path, offset and max_chars pagination.
- Supply a new stable claim_request_id for each intended claim and reuse it after an
  uncertain delivery, with the same worker, execution and work filters. The same
  authenticated key recovers the still-active lease without another attempt or renewal.
  claim_request_closed means that lease ended; inspect status and use a new ID for new work.
  Never reuse an old claim ID to request the next packet.
- carlina_status.work_delivery reports this access key's response default and
  maximum. Omit max_response_bytes to use that default; overrides cannot exceed
  the maximum. Existing keys default to 8 MiB with a 64 MiB maximum. Configure
  tighter limits for tunnels and smaller clients instead of always requesting 64 MiB.
  Oversized work responses return delivery.mode=deferred with immutable image references.
  Fetch text/schema slices and work_get(image_offset=0, image_limit=1..4), following
  delivery.next_offset until every image has been inspected. Batches shrink to the byte
  budget without resizing images. This splits transport, not the metadata task: submit
  one complete answer only after retrieving all required evidence. A singleton that
  exceeds the byte budget fails explicitly; increase the budget only if the client can
  receive it. A repeated delivery failure can be retried with a smaller byte budget.
- carlina_document_cleanup previews marked page headers/footers; save only after reviewing
  counts/examples, with expected_revision and an idempotency_key. It supports review undo.
- Work lease/submit loop: repeatedly call carlina_work_claim with the required exact
  execution_id (cross-execution leasing is never implicit, plus optional wait_seconds
  long polling and workload_role filter), answer the packet according
  to expected_output_format and request.response_format, renew its lease during long inference
  via carlina_work_renew, or release it via carlina_work_release if unable to fulfill.
  Call carlina_work_submit with the active lease token and completion payload to advance
  the workflow. Never invent a result for an expired lease.
- For JSON output pass a native object/array in response.content, or encode it once
  as a string. Never double-encode. On rejection, correct
  validation_errors paths under the same active lease; optional dry_run=true validates
  without recording a submission. Release uses work_item_id and lease_token for any
  work kind, including metadata; no expiry sleep is needed.
- accepted/valid confirms only the response contract. Product validation happens
  afterward: job_get exposes validation_error (batch, page, stage, attempt) and
  checkpoint counts. A validating batch has submitted output, not verified pages.
  Use fresh per-batch worker contexts for long image-heavy runs; the packets carry
  metadata and boundary context. Keep one claimant per sequential batch.
- Translator retries expose request.dispatch.translator_window_retry with attempt
  identity and safe validation_codes. Correct those issues in the new response,
  preserving the packet's frozen schema, source scope and protected content.
- When job_get reports suspended, inspect product.recovery, then call
  carlina_job_resume(execution_id, idempotency_key) with a new stable key for that
  resume attempt. Reuse that key after an uncertain response or connection loss.
  Resume retains the execution, frozen plan and accepted responses; continue the
  usual claim/submit/poll loop without resubmitting completed work. Changed or
  missing source/settings block resume and preserve progress. A later suspension
  needs a new key. Cancel remains explicit and cannot be undone by resume.
- If product.delivery.pending is true, the queue handoff is incomplete. Follow
  its next_action and poll the same execution with carlina_job_get to reconcile
  the saved delivery. Do not create a replacement execution. A source/settings
  blocker must be resolved before that saved attempt can run.
- Saved partial PDF pages survive failure. A new passive start cannot resume that
  checkpoint; restart_from_scratch=true explicitly discards it. Preserve the same
  execution across reconnects. Older failed executions without recovery proofs
  remain terminal. Refresh tools/list after a server upgrade
  if dry_run or detailed polling parameters are absent from the client catalog.
- Polls return compact batch counts. Request include_batches=true with batch_offset
  and batch_limit only for detailed inspection. Start/claim include_details=true and
  submit include_response=true opt into diagnostic or echoed content.
- Call carlina_job_cancel(execution_id) to stop an unwanted execution and fence its
  pending or leased work before starting another run.

Human review handoff:
- Passive PDF start skips initial crop review. Only later image-coordinate review
  may enter waiting_review and require visual confirmation in the Carlina UI;
  review details including page numbers, figure counts, and the web UI path are
  provided when needed.

MCP Tasks:
- MCP Tasks are not advertised: the official Python SDK does not yet implement that
  experimental extension, so Carlina uses these explicit durable job and work tools."""

SERVER_INSTRUCTIONS = f"{_NEWS_INSTRUCTIONS_PREAMBLE}\n\n{_SHARED_MCP_INSTRUCTIONS}"
DOCUMENT_SERVER_INSTRUCTIONS = (
    "Carlina exposes owner-scoped Document Processor and document-only Translator "
    "workflows.\n\n"
    f"{_SHARED_MCP_INSTRUCTIONS}"
)


def _is_sqlite_write_contention(exc: BaseException) -> bool:
    """Return whether an ORM commit failed only because SQLite is busy/locked."""

    if not isinstance(exc, OperationalError):
        return False
    original = exc.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False
    error_code = getattr(original, "sqlite_errorcode", None)
    return (
        isinstance(error_code, int)
        and not isinstance(error_code, bool)
        and error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


class CarlinaTokenVerifier:
    """Adapt Carlina's opaque, digest-only access keys to the SDK verifier."""

    def __init__(self, app: Flask):
        self.app = app
        self.composition = _mcp_runtime_composition(app)

    def _verify_sync(self, raw_token: str):
        from mcp.server.auth.provider import AccessToken

        from iarf_assistant.core.mcp_jobs import McpAuthError, resolve_access_key

        with self.app.app_context():
            try:
                principal = resolve_access_key(raw_token)
            except McpAuthError:
                db.session.rollback()
                return None
            except Exception as exc:  # noqa: BLE001 - token verification must fail closed
                db.session.rollback()
                # Exception text and tracebacks may include SQL parameters or
                # credential material. Expose only the failure category locally.
                logger.error(
                    "MCP access-key lookup failed (%s). Check server readiness "
                    "and startup diagnostics.",
                    type(exc).__name__,
                )
                return None
            # The principal is already verified. A contended usage-timestamp
            # write must not turn valid credentials into an authentication error.
            try:
                db.session.commit()
            except Exception as exc:  # noqa: BLE001 - token verification must fail closed
                db.session.rollback()
                if not _is_sqlite_write_contention(exc):
                    logger.error(
                        "MCP access-key usage update failed (%s). Check server "
                        "readiness and database diagnostics.",
                        type(exc).__name__,
                    )
                    return None
            return AccessToken(
                # Do not retain the bearer material in the request context.
                token="",
                client_id=principal.key_id,
                scopes=sorted(effective_mcp_scopes(principal.scopes, self.composition)),
                subject=str(principal.owner_user_id),
                claims={
                    "owner_user_id": principal.owner_user_id,
                    "allowed_roots": list(principal.allowed_roots),
                    "response_default_bytes": principal.response_default_bytes,
                    "response_max_bytes": principal.response_max_bytes,
                    "issuer": "carlina-local",
                },
            )

    async def verify_token(self, token: str):
        import anyio

        return await anyio.to_thread.run_sync(self._verify_sync, token)


def _principal_from_request(
    *, required_scope: str | None = None
) -> McpPrincipal:
    from mcp.server.auth.middleware.auth_context import get_access_token

    from iarf_assistant.core.mcp_jobs import McpAuthError, McpPrincipal

    try:
        token = get_access_token()
        if token is None or token.subject is None:
            raise McpAuthError(
                "invalid_token", "Authentication is required.", status_code=401
            )
        claims = token.claims or {}
        try:
            principal = McpPrincipal(
                key_id=str(token.client_id),
                owner_user_id=int(token.subject),
                scopes=frozenset(str(scope) for scope in token.scopes),
                allowed_roots=tuple(str(path) for path in claims.get("allowed_roots", [])),
                response_default_bytes=claims.get("response_default_bytes", DEFAULT_RESPONSE_BYTES),
                response_max_bytes=claims.get("response_max_bytes", MAX_RESPONSE_BYTES),
            )
            resolve_response_budget(
                None, default_bytes=principal.response_default_bytes,
                max_bytes=principal.response_max_bytes,
            )
        except (TypeError, ValueError) as exc:
            raise McpAuthError(
                "invalid_token",
                "The authenticated MCP principal is invalid.",
                status_code=401,
            ) from exc
        if required_scope and required_scope not in principal.scopes:
            raise McpAuthError(
                "insufficient_scope",
                f"The access key does not grant {required_scope}. Ask the local operator to grant this scope to the existing key.",
                status_code=403,
            )
        return principal
    except McpAuthError as exc:
        # Most tools check authority before entering their product handler.
        # Mark these bounded errors safe so the SDK does not mask the reason.
        raise _tool_failure(exc) from None


def _require_addon_scope(principal, addon_id: str) -> None:
    from iarf_assistant.core.mcp_jobs import MCP_ADDON_SCOPES, McpAuthError

    required_scope = MCP_ADDON_SCOPES.get(str(addon_id))
    if required_scope is None or not principal.has_scope(required_scope):
        raise McpAuthError(
            "insufficient_scope",
            "The access key does not grant this product execution.",
            status_code=403,
        )


async def _in_app[Result](app: Flask, function: Callable[[], Result]) -> Result:
    import anyio

    def invoke() -> Result:
        with app.app_context():
            try:
                return function()
            except BaseException:
                db.session.rollback()
                raise
            finally:
                db.session.remove()

    return await anyio.to_thread.run_sync(invoke)


def _tool_failure(exc: BaseException) -> Exception:
    from mcp.server.mcpserver.exceptions import ToolError

    if isinstance(exc, ToolError):
        return exc
    safe_code = str(getattr(exc, "code", "operation_failed"))[:64]
    safe_detail = str(getattr(exc, "detail", "Carlina could not complete the operation."))
    if not safe_detail or safe_detail == str(exc):
        safe_detail = str(exc).strip() or "Carlina could not complete the operation."
    if not hasattr(exc, "code"):
        logger.exception("MCP tool failed with error type=%s", type(exc).__name__)
        safe_detail = "Carlina could not complete the operation."
    if safe_code == "html-text-limit":
        safe_detail = (
            "Source HTML exceeds the planner's text-node limit before translation. "
            "Inspect the document's entity encoding and structure; changing model "
            "or translation-batch token budgets cannot resolve this limit."
        )
    return ToolError(f"{safe_code}: {safe_detail[:512]}")


def _review_edit_failure(exc: BaseException, document_id: str, expected_revision: str):
    """Keep bounded, text-free edit diagnostics available to MCP clients."""
    from mcp_types import CallToolResult, TextContent

    diagnostics = getattr(exc, "diagnostics", None)
    if getattr(exc, "code", None) != "invalid_edit" or not isinstance(
        diagnostics, list
    ):
        return None
    if not diagnostics:
        return None
    integer_fields = {
        "edit_index",
        "replacement_index",
        "expected_matches",
        "actual_matches",
        "actual_count",
        "expected_count",
        "conflicting_edit_index",
        "conflicting_replacement_index",
    }
    safe_diagnostics = []
    for diagnostic in diagnostics[:64]:
        if not isinstance(diagnostic, Mapping):
            continue
        safe = {
            key: value
            for key, value in diagnostic.items()
            if key in integer_fields and type(value) is int
        }
        element_id = diagnostic.get("element_id")
        if (
            isinstance(element_id, str)
            and 7 <= len(element_id) <= 32
            and element_id[0] == "n"
            and element_id[1:].isascii()
            and element_id[1:].isdigit()
        ):
            safe["element_id"] = element_id
        for key in ("code", "operation"):
            value = diagnostic.get(key)
            if (
                isinstance(value, str)
                and len(value) <= 80
                and value.replace("_", "").isascii()
                and value.replace("_", "").isalnum()
            ):
                safe[key] = value
        message = diagnostic.get("message")
        if isinstance(message, str):
            safe["message"] = message[:240]
        safe_diagnostics.append(safe)
    payload = {
        "document_id": document_id[:128],
        "expected_revision": expected_revision[:128],
        "changed": False,
        "error": {
            "code": "invalid_edit",
            "message": "The atomic edit batch was rejected; no changes were saved.",
            "diagnostics": safe_diagnostics,
            "diagnostics_truncated": len(diagnostics) > 64,
            "total_diagnostics": len(diagnostics),
            "index_base": 0,
        },
    }
    return CallToolResult(
        isError=True,
        content=[TextContent(text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
    )


def _loaded_addon(app: Flask, addon_id: str):
    manager = getattr(app, "addon_manager", None)
    addon = manager.get_loaded_addon(addon_id) if manager is not None else None
    if addon is None:
        raise RuntimeError(f"Required add-on '{addon_id}' is not active.")
    return addon


def _doc_service(app: Flask, owner_user_id: int) -> DocProcessorMcpService:
    from iarf_assistant.addons.doc_processor.mcp_service import DocProcessorMcpService

    addon = _loaded_addon(app, "doc_processor")
    return DocProcessorMcpService(addon.context, owner_user_id)


def _translator_client(app: Flask, owner_user_id: int) -> TranslatorClient:
    addon = _loaded_addon(app, "translator")
    return addon.initialize_client({"current_user_id": owner_user_id})


def _execution_payload(execution: McpExecution) -> dict[str, Any]:
    from iarf_assistant.core.mcp_jobs import serialize_execution

    return serialize_execution(execution)


def _start_executor_profile(
    mode: str,
    value: Mapping[str, Any] | ExecutorProfileInput | ExecutorProfile | None,
) -> ExecutorProfile | None:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"managed", "passive"}:
        raise ValueError("Execution mode is invalid.")
    if normalized_mode == "managed":
        if value is not None:
            raise ExecutorProfileError(
                "executor_profile is only accepted for passive execution."
            )
        return None
    return (
        value if isinstance(value, ExecutorProfile) else require_executor_profile(value)
    )


def _create_document_execution(
    app: Flask,
    *,
    owner_user_id: int,
    document_id: str,
    mode: str,
    executor_profile: ExecutorProfile | None = None,
    batch_plan: Mapping[str, Any] | None = None,
    restart_from_scratch: bool = False,
) -> dict[str, Any]:
    from ..addons.doc_processor.mcp_execution import start_mcp_execution

    def get_service() -> DocProcessorMcpService:
        return _doc_service(app, owner_user_id)

    def serialize(execution: McpExecution) -> dict[str, Any]:
        return _execution_payload(execution)

    return start_mcp_execution(
        owner_user_id=owner_user_id,
        document_id=document_id,
        mode=mode,
        service_factory=get_service,
        execution_payload=serialize,
        executor_profile=executor_profile,
        batch_plan=batch_plan,
        restart_from_scratch=restart_from_scratch,
    )


def _create_translator_execution(
    app: Flask,
    *,
    owner_user_id: int,
    project_id: str,
    mode: str,
    executor_profile: ExecutorProfile | None = None,
    research_dossier_revision_id: str | None = None,
    context_mode: Literal["self_contained", "retained"] | None = None,
) -> dict[str, Any]:
    from iarf_assistant.addons.translator.mcp_execution import start_mcp_execution

    def get_client() -> TranslatorClient:
        return _translator_client(app, owner_user_id)

    def serialize(execution: McpExecution) -> dict[str, Any]:
        return _execution_payload(execution)

    return start_mcp_execution(
        owner_user_id=owner_user_id,
        project_id=project_id,
        mode=mode,
        client_factory=get_client,
        execution_payload=serialize,
        executor_profile=executor_profile,
        research_dossier_revision_id=research_dossier_revision_id,
        context_mode=context_mode,
    )


def _register_product_tools(
    server, app: Flask, composition: RuntimeComposition
) -> None:
    selected_products = selected_mcp_products(composition)

    @server.tool(
        name="carlina_status",
        description=(
            "Discover Carlina's MCP protocol/profile readiness, authorized local import "
            "roots for this connection, and active product add-ons."
        ),
    )
    async def status() -> dict[str, Any]:
        principal = _principal_from_request()

        def read() -> dict[str, Any]:
            manager = getattr(app, "addon_manager", None)
            available = []
            for addon_id, scope in selected_products:
                if scope not in principal.scopes:
                    continue
                loaded = (
                    manager.get_loaded_addon(addon_id) if manager is not None else None
                )
                available.append({"addon_id": addon_id, "active": loaded is not None})
            return {
                "server": "carlina",
                "application": application_distribution_info(composition.profile),
                "authorization": {
                    "key_id": principal.key_id,
                    "granted_scopes": sorted(principal.scopes),
                    "missing_product_scopes": [
                        scope
                        for _addon_id, scope in selected_products
                        if scope not in principal.scopes
                    ],
                    "note": "The products field lists only add-ons authorized by this key. An omitted product is not evidence that its backend is disabled. The local operator can grant an explicit scope without replacing the key.",
                },
                "protocol": MCP_PROTOCOL_VERSION,
                "legacy_protocol": MCP_LEGACY_PROTOCOL_VERSION,
                "sdk": importlib.metadata.version("mcp"),
                "tasks_extension": "not_advertised",
                "work_delivery": {
                    "default_max_response_bytes": principal.response_default_bytes,
                    "max_response_bytes_range": [65536, principal.response_max_bytes],
                    "image_batch_limit": 4,
                    "claim_recovery": "claim_request_id scoped to authenticated key and execution",
                    "note": "Passive-work response budgets are configured per access key. Omit max_response_bytes to use this connection's default; overrides cannot exceed its maximum. Transport limits do not change model context admission. Fetch missing deferred images before answering.",
                },
                "local_import": {
                    "allowed_roots": list(principal.allowed_roots)
                    if "mcp.doc_processor" in principal.scopes else [],
                    "note": "Source paths must resolve to regular files within an allowed root. Import does not grant access to additional directories.",
                },
                "executor_profile": {
                    "schema_version": EXECUTOR_PROFILE_SCHEMA_VERSION,
                    "required_fields": sorted(EXECUTOR_PROFILE_KEYS),
                    "note": "Declare actual capabilities and limits for the requested passive task. No separate literal confirmation is required; user_confirmed=true acknowledges task authorization. Carlina does not identify or authenticate the external model/provider/credential.",
                },
                "products": available,
            }

        return await _in_app(app, read)

    from ..addons.doc_processor.mcp_registration import register_document_tools
    from .mcp_tool_runtime import McpToolRuntime

    def authorize_document_tool(required_scope: str) -> McpPrincipal:
        return _principal_from_request(required_scope=required_scope)

    async def run_document_tool_in_app[Result](
        app: Flask, function: Callable[[], Result]
    ) -> Result:
        return await _in_app(app, function)

    def document_tool_failure(exc: BaseException) -> Exception:
        return _tool_failure(exc)

    def document_service(
        app: Flask, owner_user_id: int
    ) -> DocProcessorMcpService:
        return _doc_service(app, owner_user_id)

    def start_document_execution(
        app: Flask,
        *,
        owner_user_id: int,
        document_id: str,
        mode: str,
        executor_profile: ExecutorProfile | None = None,
        batch_plan: Mapping[str, Any] | None = None,
        restart_from_scratch: bool = False,
    ) -> dict[str, Any]:
        return _create_document_execution(
            app,
            owner_user_id=owner_user_id,
            document_id=document_id,
            mode=mode,
            executor_profile=executor_profile,
            batch_plan=batch_plan,
            restart_from_scratch=restart_from_scratch,
        )

    def normalize_document_executor_profile(
        mode: str, value: ExecutorProfileInput | None
    ) -> ExecutorProfile | None:
        return _start_executor_profile(mode, value)

    def compact_document_start(result: dict[str, Any]) -> dict[str, Any]:
        return _compact_document_start(result)

    register_document_tools(
        server,
        McpToolRuntime(
            app=app,
            authorize=authorize_document_tool,
            in_app=run_document_tool_in_app,
            failure=document_tool_failure,
        ),
        service=document_service,
        start_execution=start_document_execution,
        normalize_executor_profile=normalize_document_executor_profile,
        compact_start=compact_document_start,
        image_result=_mcp_image_result,
        review_edit_failure=_review_edit_failure,
    )

    from ..addons.translator.mcp_registration import register_translator_tools
    from .mcp_tool_runtime import McpToolRuntime

    def authorize_translator_tool(required_scope: str) -> McpPrincipal:
        return _principal_from_request(required_scope=required_scope)

    async def run_translator_tool_in_app[Result](
        app: Flask, function: Callable[[], Result]
    ) -> Result:
        return await _in_app(app, function)

    def translator_tool_failure(exc: BaseException) -> Exception:
        return _tool_failure(exc)

    def translator_client(app: Flask, owner_user_id: int) -> TranslatorClient:
        return _translator_client(app, owner_user_id)

    def start_translator_execution(
        app: Flask,
        *,
        owner_user_id: int,
        project_id: str,
        mode: str,
        executor_profile: ExecutorProfile | None = None,
        research_dossier_revision_id: str | None = None,
        context_mode: Literal["self_contained", "retained"] | None = None,
    ) -> dict[str, Any]:
        return _create_translator_execution(
            app,
            owner_user_id=owner_user_id,
            project_id=project_id,
            mode=mode,
            executor_profile=executor_profile,
            research_dossier_revision_id=research_dossier_revision_id,
            context_mode=context_mode,
        )

    def normalize_translator_executor_profile(
        mode: str, value: Mapping[str, Any] | None
    ) -> ExecutorProfile | None:
        return _start_executor_profile(mode, value)

    def translator_execution_payload(execution: McpExecution) -> dict[str, Any]:
        return _execution_payload(execution)

    def render_translator_source_pages(
        owner_user_id: int,
        document_id: str,
        page_numbers: list[int],
        *,
        max_edge: int,
    ) -> dict[str, Any]:
        from ..addons.doc_processor.mcp_review import DocumentReviewService

        return DocumentReviewService(_doc_service(app, owner_user_id)).source_pages(
            document_id, page_numbers, max_edge=max_edge
        )

    def translator_image_result(payload: dict[str, Any]) -> CallToolResult:
        return _mcp_image_result(payload)

    register_translator_tools(
        server,
        McpToolRuntime(
            app=app,
            authorize=authorize_translator_tool,
            in_app=run_translator_tool_in_app,
            failure=translator_tool_failure,
        ),
        client=translator_client,
        start_execution=start_translator_execution,
        normalize_executor_profile=normalize_translator_executor_profile,
        execution_payload=translator_execution_payload,
        source_pages=render_translator_source_pages,
        image_result=translator_image_result,
    )


def _connection_response_budget(principal, requested: int | None) -> int:
    from .mcp_jobs import McpWorkError

    try:
        return resolve_response_budget(
            requested, default_bytes=principal.response_default_bytes,
            max_bytes=principal.response_max_bytes,
        )
    except ValueError as exc:
        raise McpWorkError("invalid_response_budget", str(exc), status_code=400) from None


def _register_job_tools(server, app: Flask) -> None:
    @server.tool(
        name="carlina_job_resume",
        description=(
            "Resume a suspended passive PDF extraction using its existing execution, "
            "frozen plan and accepted responses. Inspect carlina_job_get product.recovery "
            "first. Supply a new stable idempotency_key for each suspension; reuse it "
            "after uncertain delivery. Changed/missing sources or changed settings "
            "block recovery without discarding progress. Terminal jobs cannot resume."
        ),
    )
    async def job_resume(execution_id: str, idempotency_key: str) -> dict[str, Any]:
        principal = _principal_from_request(required_scope="mcp.work")

        def run() -> dict[str, Any]:
            from .mcp_jobs import McpWorkError, get_execution
            from ..addons.doc_processor.execution_recovery import resume_execution

            execution = get_execution(execution_id, owner_user_id=principal.owner_user_id)
            if execution is None:
                raise LookupError("MCP execution was not found.")
            _require_addon_scope(principal, execution.addon_id)
            if execution.addon_id != "doc_processor":
                raise McpWorkError("resume_unsupported", "This execution does not support recovery.", status_code=409)
            return resume_execution(
                _doc_service(app, principal.owner_user_id), execution_id, idempotency_key
            )

        try:
            return await _in_app(app, run)
        except Exception as exc:
            raise _tool_failure(exc) from exc

    @server.tool(
        name="carlina_job_get",
        description=(
            "Poll one Carlina MCP execution. Returns product truth, including "
            "waiting_external and waiting_review handoffs, with compact batch counts. "
            "Suspended PDF jobs expose product.recovery eligibility, saved-work counts and the resume call. "
            "Polling also reconciles interrupted queue handoffs; product.delivery identifies any blocker. "
            "For individual batches set include_batches=true and page with batch_offset/batch_limit. "
            "include_details=true includes full review page lists, instructions and local preparation/validation timings when available."
        ),
    )
    async def job_get(
        execution_id: str,
        include_batches: bool = False,
        batch_offset: int = 0,
        batch_limit: int = 20,
        include_details: bool = False,
    ) -> dict[str, Any]:
        principal = _principal_from_request()

        def run() -> dict[str, Any]:
            from iarf_assistant.core.mcp_jobs import (
                get_execution,
                project_execution_batches,
                transition_execution,
            )

            execution = get_execution(
                execution_id,
                owner_user_id=principal.owner_user_id,
            )
            if execution is None:
                raise LookupError("MCP execution was not found.")
            _require_addon_scope(principal, execution.addon_id)
            if execution.addon_id == "doc_processor":
                from ..addons.doc_processor.execution_recovery import reconcile_resume_dispatch

                service = _doc_service(app, principal.owner_user_id)
                recovery = (execution.request_json or {}).get("recovery", {})
                if recovery.get("attempts") or recovery.get("initial_attempt"):
                    reconcile_resume_dispatch(service, execution)
                projection = service.job_projection(execution.resource_id, execution_id=execution.id)
            elif execution.addon_id == "translator":
                projection = _translator_client(
                    app, principal.owner_user_id
                ).mcp_job_projection(execution.resource_id, execution_id=execution.id)
            elif execution.addon_id == "news_pipeline":
                news_projection = import_module(
                    "iarf_assistant.addons.news_pipeline.mcp_runtime"
                ).projection

                projection = news_projection(principal.owner_user_id, execution.id)
            else:  # The database check constraint should make this unreachable.
                raise RuntimeError("MCP execution has an unsupported product profile.")
            document_projection_matches = True
            if execution.addon_id == "doc_processor":
                product_execution = projection.get("execution_id")
                if product_execution != execution.id:
                    # Numeric preparation counters belong to the current
                    # document run; legacy unbound state cannot attribute them.
                    projection.pop("preparation_timings", None)
                document_projection_matches = (
                    product_execution is None or product_execution == execution.id
                )
                if product_execution and not document_projection_matches:
                    # A document can be reused by a later execution. Its current
                    # queue/checkpoint must not overwrite this execution's truth.
                    external_work_timing = projection.get("external_work_timing")
                    projection = {
                        "state": execution.status,
                        "current_document_state": projection.get("state"),
                        "current_document_execution_id": product_execution,
                        "ui_path": None,
                    }
                    if external_work_timing is not None:
                        projection["external_work_timing"] = external_work_timing
                elif execution.status in {"completed", "failed", "cancelled"}:
                    projection["state"] = execution.status
                    if not document_projection_matches:
                        for key in ("checkpoint", "validation_error", "completed_batch_ids"):
                            projection.pop(key, None)
            state = str(projection.get("state") or execution.status)
            if state != execution.status and execution.status not in {
                "completed",
                "failed",
                "cancelled",
            }:
                execution = transition_execution(execution.id, state)
                db.session.commit()
            batches = project_execution_batches(
                execution,
                completed_batch_ids=projection.get("completed_batch_ids", ()),
            )
            if execution.addon_id == "news_pipeline":
                news_batches = import_module(
                    "iarf_assistant.addons.news_pipeline.mcp_runtime"
                ).batches

                batches = news_batches(execution)
            if execution.addon_id == "doc_processor":
                _reconcile_document_batch_validation(
                    batches, execution.status, projection,
                    checkpoint_matches=document_projection_matches,
                )
            result = {
                "execution": _compact_execution(_execution_payload(execution)),
                "product": {
                    key: value for key, value in projection.items()
                    if key not in {"completed_batch_ids", "queue_job_id", "document_id", "execution_id", "status", "manual_review_path"}
                },
                "batch_summary": _batch_summary(batches),
            }
            if include_batches:
                offset = max(0, batch_offset)
                limit = max(1, min(100, batch_limit))
                result["batches"] = batches[offset:offset + limit]
                result["pagination"] = {
                    "offset": offset, "limit": limit, "total": len(batches),
                    "next_offset": offset + limit if offset + limit < len(batches) else None,
                }
            if not include_details:
                result["product"].pop("local_timings", None)
                result["product"].pop("preparation_timings", None)
            review = result["product"].get("review_details")
            if isinstance(review, Mapping) and not include_details:
                result["product"]["review_details"] = {
                    key: review[key] for key in ("kind", "ui_path", "figure_count") if key in review
                }
                if isinstance(review.get("pages"), list):
                    result["product"]["review_details"]["page_count"] = len(review["pages"])
            return result

        try:
            return await _in_app(app, run)
        except BaseException as exc:
            raise _tool_failure(exc) from exc

    @server.tool(
        name="carlina_job_list",
        description=(
            "List owner-scoped nonterminal MCP executions by default, with a "
            "20-item page. An explicit status overrides the active default; set "
            "include_terminal=true for unfiltered history. Filter by status (e.g. 'waiting_external' "
            "for jobs awaiting model completion, 'running', 'completed') or mode "
            "('passive', 'managed') to discover active runs, check pending work items, "
            "and resume jobs."
        ),
    )
    async def job_list(
        status: str | None = None,
        addon_id: str | None = None,
        mode: str | None = None,
        limit: int = 20,
        offset: int = 0,
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        principal = _principal_from_request()

        def run() -> dict[str, Any]:
            from iarf_assistant.core.mcp_jobs import list_executions

            executions = list_executions(
                principal,
                status=status,
                addon_id=addon_id,
                mode=mode,
                limit=min(100, max(1, int(limit))) + 1,
                offset=max(0, int(offset)),
                include_terminal=include_terminal,
            )
            requested_limit = min(100, max(1, int(limit)))
            requested_offset = max(0, int(offset))
            has_more = len(executions) > requested_limit
            return {
                "executions": executions[:requested_limit],
                "pagination": {
                    "offset": requested_offset,
                    "limit": requested_limit,
                    "has_more": has_more,
                    "next_offset": requested_offset + requested_limit
                    if has_more
                    else None,
                },
            }

        try:
            return await _in_app(app, run)
        except BaseException as exc:
            raise _tool_failure(exc) from exc

    @server.tool(
        name="carlina_job_cancel",
        description=(
            "Cancel one owner-scoped MCP execution, signal its product queue, and "
            "fence every unfinished passive-work lease."
        ),
    )
    async def job_cancel(execution_id: str) -> dict[str, Any]:
        principal = _principal_from_request(required_scope="mcp.work")

        def run() -> dict[str, Any]:
            from .mcp_jobs import (
                cancel_execution,
                get_execution,
                transition_execution,
            )

            execution = get_execution(
                execution_id,
                owner_user_id=principal.owner_user_id,
            )
            if execution is None:
                raise LookupError("MCP execution was not found.")
            _require_addon_scope(principal, execution.addon_id)
            if execution.addon_id == "doc_processor":
                product = _doc_service(
                    app, principal.owner_user_id
                ).cancel_execution(
                    execution.resource_id,
                    execution_id=execution.id,
                    queue_job_id=execution.queue_job_id,
                )
            elif execution.addon_id == "translator":
                product = _translator_client(
                    app, principal.owner_user_id
                ).cancel_mcp_execution(
                    execution.resource_id,
                    execution_id=execution.id,
                    queue_job_id=execution.queue_job_id,
                )
            elif execution.addon_id == "news_pipeline":
                cancel_news = import_module(
                    "iarf_assistant.addons.news_pipeline.mcp_runtime"
                ).cancel

                product = cancel_news(principal, execution.id)
            else:
                raise RuntimeError("MCP execution has an unsupported product profile.")
            product_state = str(
                product.get("state") or product.get("status") or ""
            ).strip().lower()
            if product_state in {"completed", "failed"}:
                execution = transition_execution(execution.id, product_state)
                db.session.commit()
                return {
                    "execution": _execution_payload(execution),
                    "product": product,
                    "cancelled": False,
                }
            if product_state != "cancelled":
                raise RuntimeError(
                    "The product did not reach a terminal state during cancellation."
                )
            cancelled = cancel_execution(principal, execution.id)
            return {
                "execution": cancelled,
                "product": product,
                "cancelled": True,
            }

        try:
            return await _in_app(app, run)
        except BaseException as exc:
            raise _tool_failure(exc) from exc

    @server.tool(
        name="carlina_work_claim",
        description=(
            "Atomically lease the oldest pending passive model-completion packet, "
            "for one required exact execution_id; cross-execution leasing is not allowed. "
            "Supports wait_seconds for long-polling until work is ready. "
            "Omit lease_seconds for packet-aware defaults: Translator receives 30-60 minutes; "
            "other products retain five minutes. Explicit durations from 30 to 3600 seconds override this. "
            "Images arrive as native content blocks. request.response_format is the output schema. "
            "For PDF vision work, the executing model must read those images and author the extraction directly; "
            "text-layer/OCR/helper-generated drafts require explicit user permission, even with later visual review. "
            "Submit response.content as text, or a native object/array for JSON output. "
            "response_mode=compact omits reusable system instructions with hashes; minimal omits task content/images; "
            "full adds diagnostics. omit_fields selectively removes system_prompt, messages, images, response_format, "
            "parameters, tools or submission_contract. Fetch missing fields with carlina_work_get under the same lease. "
            "Start each worker/stage with standard and read its exact leased contract; prefer native compact on later "
            "claims only while the executing model retains that matching contract. After compaction, transfer, restart, "
            "a hash change or doubt, call work_get with the same active work_item_id/lease_token and read omitted "
            "contract/context before authoring (hash/cache alone does not prove retention); no new claim or restart is "
            "needed. include_details is the full alias."
            " Supply a stable claim_request_id and reuse it after lost delivery to recover the same active lease. "
            "Use a new ID for each new claim. Omit max_response_bytes for the connection default shown by carlina_status; "
            "overrides must fit its configured maximum. Oversized responses defer images "
            "for work_get image batches without changing the task or image quality."
        ),
    )
    async def work_claim(
        worker_id: str,
        execution_id: str,
        addon_id: str | None = None,
        lease_seconds: int | None = None,
        wait_seconds: int = 0,
        workload_role: str | None = None,
        batch_id: str | None = None,
        include_details: bool = False,
        response_mode: str = "standard",
        omit_fields: list[str] | None = None,
        max_response_bytes: Annotated[int | None, Field(strict=True, ge=65536, le=67108864)] = None,
        claim_request_id: str | None = None,
        known_context: dict[str, str] | None = None,
        known_target_revisions: list[str] | None = None,
    ):
        from .mcp_work_projection import validate_projection

        principal = _principal_from_request(required_scope="mcp.work")
        try:
            validate_projection(response_mode, omit_fields)
            from .mcp_retained_context import validate_context_acknowledgements

            validate_context_acknowledgements(known_context, known_target_revisions)
            max_response_bytes = _connection_response_budget(principal, max_response_bytes)
        except Exception as exc:
            raise _tool_failure(exc) from exc
        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_execution_id:
            from .mcp_jobs import McpWorkError

            raise _tool_failure(
                McpWorkError(
                    "invalid_execution",
                    "A non-empty execution_id is required for work claims.",
                )
            )
        timeout = max(0, min(int(wait_seconds or 0), 60))

        def run():
            from .mcp_jobs import claim_next_work

            return claim_next_work(
                principal,
                worker_id,
                addon_id=addon_id,
                lease_seconds=lease_seconds,
                workload_role=workload_role,
                execution_id=normalized_execution_id,
                batch_id=batch_id,
                claim_request_id=claim_request_id,
            )

        deadline = time.monotonic() + timeout
        while True:
            try:
                claim = await _in_app(app, run)
                if claim is not None or time.monotonic() >= deadline:
                    return _work_claim_result(
                        claim, include_details=include_details, response_mode=response_mode,
                        omit_fields=omit_fields, max_response_bytes=max_response_bytes,
                        known_context=known_context, known_target_revisions=known_target_revisions,
                    )
            except Exception as exc:  # noqa: BLE001 - redact arbitrary delivery failures at the protocol boundary
                raise _work_delivery_failure(exc, normalized_execution_id) from None

            await asyncio.sleep(min(0.5, max(0.1, deadline - time.monotonic())))

    @server.tool(
        name="carlina_work_get",
        description=(
            "Read the exact current leased packet without another claim, attempt or renewal. Owner and active lease required. "
            "response_mode and omit_fields match work_claim. Set path to a JSON pointer inside /request for targeted retrieval "
            "(for example /request/messages/0 or /request/dispatch/context_artifact). Large fields use offset/max_chars "
            "pagination, with hash and next_offset. Fetch an individual image content-block path for a native image."
            " Alternatively set image_offset and image_limit (1-4) for ordered, full-quality native image batches. "
            "Fetch only missing deferred images and follow delivery.next_offset until null. "
            "Batches shrink to max_response_bytes (omit for the connection default from carlina_status; "
            "overrides cannot exceed the connection maximum). "
            "An oversized single image returns work_image_too_large. Do not submit until all required evidence is retrieved; "
            "after compaction, transfer, restart, hash change or doubt, use the same active work_item_id/lease_token to "
            "get and read missing fields (hash/cache alone is not proof; no new claim or restart is needed)."
        ),
    )
    async def work_get(
        work_item_id: str, lease_token: str, response_mode: str = "standard",
        omit_fields: list[str] | None = None, path: str | None = None,
        offset: int = 0, max_chars: int = 12000,
        image_offset: Annotated[int | None, Field(strict=True, ge=0)] = None,
        image_limit: Annotated[int, Field(strict=True, ge=1, le=4)] = 1,
        max_response_bytes: Annotated[int | None, Field(strict=True, ge=65536, le=67108864)] = None,
        known_context: dict[str, str] | None = None,
        known_target_revisions: list[str] | None = None,
    ):
        from .mcp_jobs import get_leased_work
        from .mcp_work_projection import request_slice, validate_projection

        principal = _principal_from_request(required_scope="mcp.work")
        try:
            validate_projection(response_mode, omit_fields)
            from .mcp_retained_context import validate_context_acknowledgements

            validate_context_acknowledgements(known_context, known_target_revisions)
            if (path is not None or image_offset is not None) and (known_context or known_target_revisions):
                raise ValueError("Context acknowledgements cannot be combined with path or image recovery.")
            max_response_bytes = _connection_response_budget(principal, max_response_bytes)
            if image_offset is not None and (path is not None or offset != 0 or omit_fields or response_mode != "standard"):
                raise ValueError("image_offset cannot be combined with path, offset, omit_fields or a nonstandard response_mode.")
            if image_offset is None and image_limit != 1:
                raise ValueError("image_limit requires image_offset.")
            packet = await _in_app(app, lambda: get_leased_work(principal, work_item_id, lease_token))
            wire_packet = _work_wire_payload(packet)
            if image_offset is not None:
                return _prepared_work_result(_work_image_batch_result(wire_packet, image_offset, image_limit, max_response_bytes))
            if path is not None:
                return _prepared_work_result(_bounded_work_result(
                    request_slice(wire_packet, path, offset, max_chars), max_response_bytes,
                    allow_defer=False,
                ))
            return _work_claim_result(
                packet, response_mode=response_mode, omit_fields=omit_fields, max_response_bytes=max_response_bytes,
                known_context=known_context, known_target_revisions=known_target_revisions,
            )
        except Exception as exc:  # noqa: BLE001 - redact arbitrary delivery failures at the protocol boundary
            raise _work_delivery_failure(exc, work_item_id) from None

    @server.tool(
        name="carlina_work_release",
        description="Release any active document or translation lease, including metadata, using work_item_id and lease_token from its claim. Optional reason explains the handoff. Reclaim immediately; no expiry wait is needed.",
    )
    async def work_release(
        work_item_id: str,
        lease_token: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        principal = _principal_from_request(required_scope="mcp.work")

        def run():
            from .mcp_jobs import release_work

            return release_work(
                principal,
                work_item_id,
                lease_token,
                reason=reason,
            )

        try:
            return await _in_app(app, run)
        except BaseException as exc:
            raise _tool_failure(exc) from exc

    @server.tool(
        name="carlina_work_renew",
        description="Renew the exact current passive-work lease before it expires.",
    )
    async def work_renew(
        work_item_id: str,
        lease_token: str,
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        principal = _principal_from_request(required_scope="mcp.work")

        def run():
            from .mcp_jobs import renew_work

            return renew_work(
                principal,
                work_item_id,
                lease_token,
                lease_seconds=lease_seconds,
            )

        try:
            renewal = await _in_app(app, run)
            if isinstance(renewal, Mapping):
                return dict(renewal)
            raise TypeError("The renewed MCP work projection is invalid.")
        except BaseException as exc:
            raise _tool_failure(exc) from exc

    @server.tool(
        name="carlina_work_submit",
        description=(
            "Submit one assistant completion under the current lease. Carlina "
            "checks the response contract before later product validation. status=accepted is a "
            "schema-valid receipt, NOT confirmation that targets were committed; inspect execution status "
            "and any block_failure before continuing. For JSON output, "
            "pass response.content as a native object/array or one JSON-encoded string, "
            "matching request.response_format. Prefer native JSON and preserve Unicode exactly; "
            "never ASCII-fold or transliterate source text to solve encoding errors. "
            "PDF vision completions require direct model authoring from the supplied native images; "
            "text-layer/OCR/helper-generated drafts require explicit user permission. "
            "Other output uses a string. "
            "dry_run=true validates without recording or accepting a submission. Rejections "
            "identify invalid fields and whether the lease remains active. "
            "Acknowledgements omit the submitted text unless include_response=true."
        ),
    )
    async def work_submit(
        work_item_id: str,
        lease_token: str,
        response: CompletionResponse,
        dry_run: bool = False,
        include_response: bool = False,
    ) -> dict[str, Any]:
        principal = _principal_from_request(required_scope="mcp.work")
        try:
            result = await _in_app(
                app,
                lambda: __import__(
                    "iarf_assistant.core.mcp_jobs",
                    fromlist=["submit_work"],
                ).submit_work(
                    principal,
                    work_item_id,
                    lease_token,
                    response,
                    dry_run=dry_run,
                ),
            )
            if not include_response:
                result.pop("response", None)
            return result
        except BaseException as exc:
            raise _tool_failure(exc) from exc


def _compact_execution(execution: Mapping[str, Any]) -> dict[str, Any]:
    return {key: execution[key] for key in (
        "execution_id", "addon_id", "resource_id", "mode", "status",
    ) if key in execution}


def _batch_summary(batches: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(batches),
        "states": dict(Counter(batch["state"] for batch in batches)),
    }


def _reconcile_document_batch_validation(
    batches: list[dict[str, Any]], execution_status: str, projection: Mapping[str, Any],
    *, checkpoint_matches: bool,
) -> None:
    """Keep accepted model responses distinct from product-validated pages."""
    validated = set(projection.get("completed_batch_ids", ())) if checkpoint_matches else set()
    error = projection.get("validation_error")
    failed_batch = error.get("batch_id") if isinstance(error, Mapping) else None
    terminal = execution_status in {"failed", "cancelled"}
    for batch in batches:
        batch_id = batch.get("batch_id")
        counts = batch.get("counts") if isinstance(batch.get("counts"), Mapping) else {}
        state = batch.get("state")
        if batch_id in validated or execution_status == "completed":
            batch["state"] = "completed"
        elif failed_batch == batch_id and terminal:
            batch["state"] = execution_status
        elif state == "completed":
            batch["state"] = "unverified" if terminal else "validating"
        elif not counts.get("total", 0) and terminal:
            batch["state"] = "blocked"
    by_id = {
        batch_id: batch
        for batch in batches
        if isinstance((batch_id := batch.get("batch_id")), str) and batch_id
    }
    for batch in batches:
        dependencies = batch.get("depends_on")
        if not isinstance(dependencies, list):
            dependencies = []
        if batch.get("state") == "planned" and any(
            dependency not in by_id or by_id[dependency].get("state") != "completed"
            for dependency in dependencies
        ):
            batch["state"] = "blocked"


def _compact_document_start(result: dict[str, Any]) -> dict[str, Any]:
    execution = result["execution"]
    compact = {
        "execution": _compact_execution(execution),
        "state": result.get("state"),
    }
    plan = result.get("batch_plan")
    if isinstance(plan, Mapping):
        compact["batch_summary"] = {
            "total": len(plan["batches"]), "scheduling_mode": plan["scheduling_mode"],
        }
    if "orchestration" in result:
        compact["next_action"] = {
            "tool": "carlina_work_claim", "execution_id": execution["execution_id"],
            "wait_seconds": 20,
        }
    elif result.get("document"):
        document = result["document"]
        compact["document"] = {
            key: document[key] for key in ("id", "status", "review_kind", "ui_path")
            if key in document
        }
    return compact


def _work_claim_result(
    claim: Any | None, *, include_details: bool = False,
    response_mode: str = "standard", omit_fields: list[str] | None = None,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    known_context: dict[str, str] | None = None,
    known_target_revisions: list[str] | None = None,
):
    from mcp_types import CallToolResult, TextContent

    if claim is None:
        payload = {"claimed": False, "message": "No passive work is currently ready."}
        return CallToolResult(
            content=[TextContent(text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
        )

    payload = _work_wire_payload(claim)
    original_request = payload.get("request", {})
    if not (include_details or response_mode == "full"):
        request = payload.get("request") or {}
        compact = {
            key: payload[key] for key in (
                "work_item_id", "execution_id", "lease_token", "lease_expires_at",
                "workload_role", "expected_output_format", "batch_id",
            ) if key in payload
        }
        compact["request"] = {
            key: request[key] for key in (
                "messages", "tools", "response_format", "parameters", "submission_contract",
            ) if key in request
        }
        dispatch = request.get("dispatch")
        if isinstance(dispatch, Mapping):
            context_dispatch = {key: dispatch[key] for key in (
                "translator_context_mode", "translator_window_budget", "translator_window_retry",
                "extraction_contract",
            ) if key in dispatch}
            if context_dispatch:
                compact["request"]["dispatch"] = context_dispatch
        payload = compact
    from .mcp_work_projection import project_claim

    return _prepared_work_result(_bounded_work_result(
        project_claim(
            payload, response_mode, omit_fields,
            known_context=known_context, known_target_revisions=known_target_revisions,
        ), max_response_bytes,
        original_request=original_request,
    ))


def _work_wire_payload(claim: Any) -> dict[str, Any]:
    """Make added submission guidance equally retrievable by projection and path."""
    payload = claim.to_dict() if hasattr(claim, "to_dict") else dict(claim)
    request = dict(payload.get("request") or {})
    response_format = request.get("response_format") or {}
    response_format_type = response_format.get("type")
    existing_contract = request.get("submission_contract")
    contract = dict(existing_contract) if isinstance(existing_contract, Mapping) else {}
    if response_format_type == "json_schema":
        guidance = "Submit response={\"content\": <JSON object or array>}; one JSON-encoded string is also accepted. Do not double-encode."
    elif response_format_type == "json_object":
        guidance = "Submit response={\"content\": <JSON object>}; one JSON-encoded object string is also accepted. Do not double-encode."
    else:
        guidance = "Submit response={\"content\": \"<assistant text>\"}."
    contract["content"] = guidance
    contract["validation"] = "accepted/valid checks the response contract only; poll carlina_job_get for product validation and retries."
    if request.get("tools") and "tool_calls" not in contract:
        contract["tool_calls"] = "Tool-call completions may submit tool_calls according to the frozen request."
    request["submission_contract"] = contract
    payload["request"] = request
    return payload


def _work_delivery_failure(exc: BaseException, correlation_id: str):
    """Preserve typed errors, but never log prompts, tokens or exception values."""
    from .mcp_jobs import McpWorkError

    logger.warning("MCP work delivery failure correlation_id=%s error_type=%s", correlation_id, type(exc).__name__)
    if not hasattr(exc, "code"):
        exc = McpWorkError(
            "work_delivery_failed",
            "Work delivery failed. If claim_request_id was supplied, retry that ID with response_mode='minimal', "
            "then fetch text slices and image_offset/image_limit batches. A legacy claim without an ID may remain "
            "leased until expiry. Inspect the existing execution; do not start a replacement.",
            status_code=500, retryable=True,
        )
    return _tool_failure(exc)


def _work_result_size(result: Any) -> int:
    # Leave room for the SDK's JSON-RPC envelope and request identifier.
    return len(result.model_dump_json(by_alias=True).encode("utf-8")) + 1024


def _prepared_work_result(result: Any):
    payload = result.structured_content or {}
    logger.info(
        "MCP work response prepared execution_id=%s work_item_id=%s response_bytes=%d image_count=%d delivery_mode=%s",
        payload.get("execution_id"), payload.get("work_item_id"), _work_result_size(result),
        sum(block.type == "image" for block in result.content),
        (payload.get("delivery") or {}).get("mode", "inline"),
    )
    return result


def _bounded_work_result(
    payload: dict[str, Any], budget: int, *, allow_defer: bool = True,
    original_request: dict[str, Any] | None = None,
):
    from .mcp_jobs import McpWorkError
    from .mcp_work_projection import deferred_request, project_claim, request_images

    validate_response_budget(budget)
    images = request_images(payload)
    all_images = images if original_request is None else request_images(original_request)
    # Detect a large image body before decoding/copying it into SDK models.
    encoded_bytes = sum(len(block.get("image_url", {}).get("url", "")) for _, block in images)
    result = None if encoded_bytes > budget else _mcp_image_result(payload, max_image_bytes=budget)
    required_bytes = _work_result_size(result) if result is not None else encoded_bytes
    if result is not None and required_bytes <= budget:
        return result
    if not allow_defer:
        raise McpWorkError(
            "work_response_too_large",
            f"This response requires at least {required_bytes} bytes; max_response_bytes is {budget}. "
            "Use smaller text slices or image batches; increase the byte budget only if the client supports it.",
            status_code=413,
        )
    deferred = project_claim(payload, "standard", ["images"])
    deferred["projection"]["mode"] = (payload.get("projection") or {}).get("mode", "standard")
    delivery = {
        "mode": "deferred", "reason": "response_byte_budget", "max_response_bytes": budget,
        "requested_bytes_lower_bound": required_bytes, "image_count": len(all_images),
        "next_action": {"tool": "carlina_work_get", "image_offset": 0, "image_limit": 1},
        "guidance": "Transport was split without changing the task. Retrieve all omitted instructions, schema and images before submitting one complete response. Image bytes and order are unchanged.",
    }
    deferred["delivery"] = delivery
    result = _mcp_image_result(deferred)
    if _work_result_size(result) > budget:
        full_payload = {**payload, "request": original_request} if original_request is not None else payload
        deferred = deferred_request(full_payload)
        deferred["delivery"] = {**delivery, "next_action": {"tool": "carlina_work_get", "path": "/request", "max_chars": 12000}}
        result = _mcp_image_result(deferred)
    if _work_result_size(result) > budget:
        raise McpWorkError("work_response_too_large", "Even the minimal work receipt exceeds the response budget.", status_code=413)
    logger.info(
        "MCP work response deferred execution_id=%s work_item_id=%s image_count=%d response_bytes=%d budget_bytes=%d",
        payload.get("execution_id"), payload.get("work_item_id"), len(images), _work_result_size(result), budget,
    )
    return result


def _work_image_batch_result(payload: dict[str, Any], offset: int, limit: int, budget: int):
    from .mcp_jobs import McpWorkError
    from .mcp_work_projection import request_images

    validate_response_budget(budget)
    images = request_images(payload.get("request", {}))
    if type(offset) is not int or not 0 <= offset <= len(images) or type(limit) is not int or not 1 <= limit <= 4:
        raise McpWorkError("invalid_image_range", "image_offset must be within the image count and image_limit must be 1-4.", status_code=400)
    selected: list[dict[str, Any]] = []

    def response():
        end = offset + len(selected)
        return _mcp_image_result({
            **{key: payload[key] for key in ("work_item_id", "execution_id", "lease_expires_at") if key in payload},
            "images": selected,
            "delivery": {
                "mode": "image_batch", "image_count": len(images), "image_offset": offset,
                "returned_count": len(selected), "next_offset": end if end < len(images) else None,
                "max_response_bytes": budget,
                "guidance": "Forward native images to the model. Follow next_offset until all images are inspected; then answer the original complete packet.",
            },
        }, max_image_bytes=budget)

    accepted = response()
    for index in range(offset, min(offset + limit, len(images))):
        path, block = images[index]
        selected.append({"index": index, "path": path, "image": block})
        encoded_bytes = sum(len(item["image"]["image_url"].get("url", "")) for item in selected)
        candidate = None if encoded_bytes > budget else response()
        size = encoded_bytes if candidate is None else _work_result_size(candidate)
        if size > budget:
            selected.pop()
            if not selected:
                raise McpWorkError(
                    "work_image_too_large",
                    f"Image {index} alone requires at least {size} response bytes; max_response_bytes is {budget}. "
                    "Increase the budget only if the client supports it; the source image has not been downsampled.",
                    status_code=413,
                )
            break
        assert candidate is not None
        accepted = candidate
    return accepted


def _mcp_image_result(
    claim: Any, *, max_image_bytes: int = MAX_MCP_IMAGE_BYTES
) -> CallToolResult:
    """Share native image transport between passive packets and source review."""
    from mcp_types import CallToolResult, ImageContent, TextContent

    if hasattr(claim, "to_dict"):
        safe_claim = claim.to_dict()
    elif isinstance(claim, Mapping):
        safe_claim = dict(claim)
    else:
        raise TypeError("The MCP image projection is invalid.")
    images: list[ImageContent] = []
    total_bytes = 0

    def replace(value: Any) -> Any:
        nonlocal total_bytes
        if isinstance(value, list):
            return [replace(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") == "image_url" and isinstance(
            value.get("image_url"), dict
        ):
            image_url = dict(value["image_url"])
            url = image_url.get("url")
            file_path = value.get("file_path") or image_url.get("file_path")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                header, encoded = url.split(",", 1)
                media_type = header[5:].split(";", 1)[0].strip().lower()
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except binascii.Error, ValueError:
                    return {key: replace(item) for key, item in value.items()}
                total_bytes += len(raw)
                if (
                    len(raw) > max_image_bytes
                    or total_bytes > MAX_MCP_IMAGE_TOTAL_BYTES
                ):
                    raise ValueError("Images exceed the MCP transfer limit.")
                index = len(images)
                images.append(ImageContent(data=encoded, mimeType=media_type))
                image_url["url"] = f"carlina-mcp-image:{index}"
                if file_path:
                    image_url["file_path"] = str(file_path)
                res = {**value, "image_url": image_url}
                if file_path:
                    res["file_path"] = str(file_path)
                return res
        return {key: replace(item) for key, item in value.items()}

    safe_claim = replace(safe_claim)
    text_payload = json.dumps(safe_claim, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(text=text_payload), *images],
        structuredContent=safe_claim,
    )


def _register_application_source_resource(
    server: Any, composition: RuntimeComposition, digest: str
) -> None:
    from mcp.server.mcpserver.exceptions import ResourceError

    @server.resource(
        f"carlina://application/source/{digest}",
        name="Carlina Documents application source",
        description="The verified source archive for this installed application build.",
        mime_type="application/gzip",
    )
    def application_source_resource() -> bytes:
        _principal_from_request()
        try:
            return application_source_for_digest(composition.profile, digest).archive
        except LookupError, ValueError:
            raise ResourceError(_APPLICATION_SOURCE_UNAVAILABLE) from None


def _register_application_companions_resource(
    server: Any, composition: RuntimeComposition
) -> None:
    from mcp.server.mcpserver.exceptions import ResourceError

    @server.resource(
        APPLICATION_COMPANIONS_RESOURCE,
        name="Carlina Documents dependency sources and notices",
        description="Version-bound download metadata for selected dependency companions.",
        mime_type="application/json",
    )
    def application_companions_resource() -> str:
        _principal_from_request()
        info = application_distribution_info(composition.profile)
        companions = info["companions"]
        if not isinstance(companions, dict) or companions.get("available") is not True:
            raise ResourceError("Application companions are unavailable.")
        return json.dumps(companions, sort_keys=True)


def create_mcp_application(app: Flask, *, host: str, port: int):
    """Build the SDK-owned ASGI application; no custom JSON-RPC is involved."""

    composition = _mcp_runtime_composition(app)
    selected_products = selected_mcp_products(composition)
    instructions = (
        SERVER_INSTRUCTIONS
        if any(addon_id == "news_pipeline" for addon_id, _scope in selected_products)
        else DOCUMENT_SERVER_INSTRUCTIONS
    )
    application_info = application_distribution_info(composition.profile)
    source_info = application_info["source"]
    source_digest = None
    if isinstance(source_info, dict) and source_info.get("available") is True:
        source_digest = str(source_info["sha256"])
        instructions += (
            "\nApplication source for this installed Documents version and revision "
            f"is available at carlina://application/source/{source_digest}. "
            "carlina_status.application records the version, revision, byte length "
            "and SHA-256. This archive covers the application; native dependency "
            "source and notices have separate distribution requirements.\n"
        )

    companions_info = application_info["companions"]
    companions_available = (
        isinstance(companions_info, dict) and companions_info.get("available") is True
    )
    if companions_available:
        instructions += (
            "\nDependency source and third-party notice download metadata is available "
            f"at {APPLICATION_COMPANIONS_RESOURCE}. It includes version-bound URLs, "
            "byte lengths, SHA-256 hashes and inventory bindings. Coverage is selected; "
            "these downloads do not certify complete licensing or source coverage.\n"
        )

    from mcp.server import MCPServer
    from mcp.server.auth.settings import AuthSettings

    endpoint = f"http://{host}:{int(port)}/mcp"
    server = MCPServer(
        "carlina",
        title="Carlina Document and Translation Workflows",
        description="Owner-scoped product workflows and passive model dispatch.",
        instructions=instructions,
        version=importlib.metadata.version("carlina"),
        token_verifier=CarlinaTokenVerifier(app),
        auth=AuthSettings(
            issuer_url=f"http://{host}:{int(port)}",
            resource_server_url=endpoint,
            required_scopes=[MCP_ACCESS_SCOPE],
        ),
    )
    _register_product_tools(server, app, composition)
    if any(addon_id == "news_pipeline" for addon_id, _scope in selected_products):
        from .mcp_news import register_news_tools

        register_news_tools(
            server,
            app,
            principal_from_request=_principal_from_request,
            in_app=_in_app,
            failure=_tool_failure,
            loaded_addon=_loaded_addon,
        )
    _register_job_tools(server, app)

    if source_digest is not None:
        _register_application_source_resource(server, composition, source_digest)
    if companions_available:
        _register_application_companions_resource(server, composition)

    @server.resource(
        "carlina://guide/passive-work",
        name="Carlina passive-work guide",
        description="The durable claim/lease/submit loop and UI review boundary.",
        mime_type="text/markdown",
    )
    def passive_work_guide() -> str:
        _principal_from_request()
        return instructions

    @server.resource(
        "carlina://executions/{execution_id}",
        name="Carlina MCP execution",
        description="A safe owner-scoped execution projection.",
        mime_type="application/json",
    )
    async def execution_resource(execution_id: str) -> str:
        principal = _principal_from_request()

        def read() -> str:
            from iarf_assistant.core.mcp_jobs import get_execution

            execution = get_execution(
                execution_id,
                owner_user_id=principal.owner_user_id,
            )
            if execution is None:
                raise LookupError("MCP execution was not found.")
            _require_addon_scope(principal, execution.addon_id)
            return json.dumps(_execution_payload(execution), ensure_ascii=False)

        return await _in_app(app, read)

    @server.prompt(
        name="carlina_passive_worker",
        description="Operate Carlina's passive completion lease loop safely.",
    )
    def passive_worker_prompt(addon_id: str = "") -> str:
        _principal_from_request(required_scope="mcp.work")
        profile = f" for {addon_id}" if addon_id else ""
        return (
            f"Act as the passive Carlina worker{profile}. Claim one work item, "
            "before passive start declare the actual executor_profile limits and "
            "capabilities (schema carlina.external-executor.v1). The user's task "
            "request authorizes execution; no separate literal confirmation is "
            "required. user_confirmed=true acknowledges that task authorization. "
            "Use the exact canonical shapes: executor_profile={\"schema_version\":\"carlina.external-executor.v1\",\"max_input_tokens\":256000,\"max_output_tokens\":16000,\"input_modalities\":[\"text\",\"image\"],\"supports_tools\":true,\"supports_structured_output\":true,\"supports_reasoning\":true,\"user_confirmed\":true}; "
            "for a passive PDF, batch_plan={\"schema_version\":\"carlina.document-batches.v1\",\"scheduling_mode\":\"sequential\",\"batches\":[{\"batch_id\":\"pages-001-004\",\"start_page\":1,\"end_page\":4}]}. "
            "New document_batch completions return JSON pages entries with page_number and html, "
            "one per supplied page in order. Respect the claimed schema for legacy runs too. "
            "Use singular modality names text, image, pdf, and audio; the plan "
            "covers the whole selected page set derived from source.page_count. "
            "A worker "
            "does not need source access; do not invent aliases or guess rejected "
            "shapes. "
            "Carlina does not identify or authenticate the external model, "
            "provider, or credential. Use standard for the first claim in each worker/stage and read the exact leased "
            "contract before authoring. Prefer native compact only for later claims while the executing model still "
            "retains those instructions; hashes or wrapper/JavaScript caches alone do not prove that. After compaction, "
            "transfer, restart, hash change or doubt, call work_get with the same work_item_id and lease_token for "
            "missing instructions/schema/context, then read it before authoring; no new claim or restart is needed. "
            "Wrappers must forward omission notices, hashes, retrieval paths, and request.dispatch.extraction_contract "
            "when present into the executing model's context; keeping its summary only in wrapper/script memory does "
            "not satisfy the contract. Perform the assistant completion described by its exact messages, "
            "tools, response format, parameters, and image blocks, then submit "
            "only your assistant response using the current lease token. JSON belongs "
            "inside response.content as a native object/array or one encoded string. "
            "Acceptance checks the response contract and supported packet preflight, not target activation. Poll for product validation. Correct validation_errors "
            "under the same active lease; dry_run=true checks without accepting. Renew "
            "the lease before expiry. For Translator, first inspect source metadata, outline "
            "and bounded excerpts; use external research tools when useful and optionally "
            "submit/pin a research dossier before start. During translation use "
            "carlina_translator_glossary_get for relevant concepts and revision fences; "
            "save changed terms through carlina_translator_glossary_propose before "
            "submitting the current window when practical. Use source_plan_id, a stable "
            "idempotency_key, canonical citation forms and contextual senses. Exact quotes "
            "with block IDs anchor inflected forms without manual offset counting. "
            "For edits include concept_id and expected_revision. Read returned items "
            "to verify persistence; no full glossary reread or new dossier per batch. "
            "New packets include relevant advice while existing packet snapshots remain fixed. "
            "Before a passive run, use shared_glossary_list/get to choose one exact shared "
            "revision and preview it for the project. At an idle boundary, link pins the asset for "
            "later reviewed updates; update adopts a selected revision of the linked asset. "
            "Link/update require incoming/local decisions for every changed row. Copy selects an "
            "incoming subset as independent terms and retains any existing link; "
            "imports never rewrite target text or change acquired packets. During a run, local "
            "glossary proposals may shape future packets. Shared contributions and owner review "
            "may be prepared while work runs, but never change packets already acquired; reconcile "
            "the accepted revision in a later run. Shared publication and acceptance stay advisory "
            "and create no human approval or human locks. Never bypass a local human-locked term. "
            "Human locks override model advice. Linguistic sentence boundaries are yours; "
            "preserve markup, references, nonempty units and complete N:M coverage. "
            "Use translation_progress counters rather than operation-stage percentages. "
            "Do not add glossary fields to the translation response or claim "
            "human approval. Reader annotations remain separate. A claim with "
            "no ready work is not completion: poll the exact execution with "
            "carlina_job_get and continue until its terminal outcome."
        )

    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        # Modern 2026-07-28 requests are sessionless regardless. Keeping the
        # legacy manager stateful preserves 2025-11-25 initialize/session/SSE
        # clients (including harnesses that have not moved to discovery yet).
        stateless_http=False,
        max_request_body_size=16 * 1024 * 1024,
        host=host,
    )


__all__ = [
    "MCP_ACCESS_SCOPE",
    "MCP_DEFAULT_SCOPES",
    "MCP_LEGACY_PROTOCOL_VERSION",
    "MCP_PROTOCOL_VERSION",
    "CarlinaTokenVerifier",
    "create_mcp_application",
    "mcp_default_scopes",
    "mcp_effective_scopes",
]
