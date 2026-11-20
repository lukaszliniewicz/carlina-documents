# Passive document extraction

## Start from product truth

Call `carlina_status` once to inspect `local_import.allowed_roots` when the live
server advertises it. Resolve the actual file format as well as its name. Native
FB2 support may include a ZIP-wrapped `.fb2` containing exactly one FB2 payload;
check the current server contract instead of assuming an arbitrary ZIP is an
accepted book. Do not widen the connection's import authority as a recovery
shortcut. If access is denied, explain the specific restriction and use only
an authorized source/intake path.

Resolve the document with list/get and reuse it when identity is unambiguous.
Import only from an authorized source path. For PDFs, use `source.page_count`
to derive the intended physical page set. Configure only advertised properties
and inspect their effective values; do not probe guessed settings during a run.

Metadata prepass, initial cropping, extracted-image boxes, and visual boundary
correction are separate stages. Passive extraction defaults to full pages with
initial cropping off. Keep visual boundary correction opt-in unless requested;
a later image-coordinate review is not initial cropping. Resolve contradictory
effective settings before starting.

## Native-vision authoring

For PDF vision extraction, the executing model must read each requested page's
native image blocks and directly author its text and semantic HTML. Do not use
embedded PDF text (for example PyMuPDF `get_text` or `pdftotext`), external OCR,
or scripts that reconstruct paragraphs, headings, lists or notes to generate
extraction drafts unless the user explicitly permits that alternative. Reviewing
such drafts against page images afterward does not satisfy this workflow.

Helpers may render or crop images, assemble/transport model-authored packets,
validate markup and completeness, and package or save exports. Carlina-supplied
native-text candidate catalogs and prior-page context may guide navigation and
continuity; they do not replace reading the source image or authoring the page.
If native images cannot be viewed, retrieve the missing image blocks or report
the specific limitation. Do not silently fall back to a text-layer/OCR helper.

When asked to do the extraction yourself, the parent performs this visual
authoring. Report any explicitly permitted alternative method accurately;
schema acceptance and delivery of images do not certify the method used.

New metadata packets distinguish note placement from numbering scope in
`note_arrangement`, with bounded page-backed evidence and the governing heading
level when established. Keep unknowns explicit; a sample containing no notes or
one label of 1 does not establish document-wide absence or a restart. Full-note
packets now allow unique opaque IDs and separate printed labels, including
symbols and punctuation. Repeated references can share a body target; each
marker occurrence still needs its own ID. Link only an evidenced relationship.
Do not rewrite an older leased packet's schema during recovery. EPUB export can
place local notes after their chapter when metadata and links establish ownership;
inspect the delivered artifact rather than assuming every note was regrouped.

## Declare the executor profile

Describe the actual executor and its limits. Use 256,000 input tokens as the
default when preparing a new profile. Honor an explicitly supplied smaller limit
or a lower executor capacity. Send the value explicitly; the API does not fill
in missing capability fields. This example does not establish the current model's capacity:

```json
{
  "schema_version": "carlina.external-executor.v1",
  "max_input_tokens": 256000,
  "max_output_tokens": 16000,
  "input_modalities": ["text", "image"],
  "supports_tools": true,
  "supports_structured_output": true,
  "supports_reasoning": true,
  "user_confirmed": true
}
```

Use singular modality names: `text`, `image`, `pdf`, `audio`. Set
`user_confirmed=true` for passive work the user has requested or otherwise
authorized. The existing API requires this compatibility field; it does not
require a separate confirmation exchange or establish that the user verified
the capability claims. Declare capabilities from the actual executor, which the
server cannot authenticate. Reuse an unchanged profile after a crash or
reconnect. Increasing the input budget does not increase the output budget.
If the executor changes, update the declared profile for future work and respect
the frozen limits of any existing execution.

## Design the PDF batch plan

Use schema `carlina.document-batches.v1`. Ranges are inclusive, physical,
one-based, ordered, and non-overlapping. Cover the entire intended page set
exactly once, within the advertised maximum number of batches.

Budget each actual multi-page packet for **all** images, prompts, schema, shared
context, and the combined HTML output. Current planning estimates charge 8,192
input tokens per PDF page image; follow the live contract if that changes.
Metadata preparation has its own budget and must finish before dependent page
work. Dense pages or figures may require smaller batches even with ample input.
The default metadata window is 20 start pages. Its images alone cost 163,840
estimated tokens, so it cannot fit a 128,000-token executor. Start rejects an
impossible initial image budget before queuing; full packet admission also
checks prompt/schema overhead. Required initial metadata must fit in full; it
is not silently trimmed. Configure that window to fit the declared profile.

New budget-aware executions bound optional TOC/heading evidence to the remaining
input budget. Limited evidence or a recoverable optional refinement failure can
leave valid primary metadata with a provisional heading contract, allowing page
work to continue. Retained older executions keep their original semantics;
reconnecting does not migrate them to this fallback policy.

Example for an eight-page document, subject to the executor's output capacity:

```json
{
  "schema_version": "carlina.document-batches.v1",
  "scheduling_mode": "sequential",
  "batches": [
    {"batch_id": "pages-001-004", "start_page": 1, "end_page": 4},
    {"batch_id": "pages-005-008", "start_page": 5, "end_page": 8}
  ]
}
```

Prefer sequential scheduling for continuous prose and list-heavy documents.
Keep related nested-list pages in one batch when the actual packet/output budget
allows; a batch boundary must not become a numbering restart. For a long image-heavy run,
use one fresh worker context per batch when delegation is authorized. The packet
already contains metadata and the preceding batch's boundary handoff; retaining
the full book conversation accumulates images without improving that contract.
Concurrency can improve throughput but does not relax completeness or boundary
review requirements. Assign each worker one exact execution/batch; perform the
shared metadata pass once. The parent tracks leases and reconciles every batch.

Call `carlina_document_start` with the exact document ID, `mode="passive"`, the
declared executor profile, and the complete PDF batch plan. Retain the returned
execution ID. Local preparation may precede the first available work item.
Before assigning a page worker, inspect job state and the requested batch's
pending/reclaimable state. Metadata may require both primary and refinement
completions; waiting for their validation is not evidence of a stuck page batch.

## Drain work safely

1. Call `carlina_work_claim` with a stable worker ID and the exact execution ID.
   Supply a new stable `claim_request_id` for each intended claim, reusing it
   with unchanged selection arguments after uncertain delivery. Do not reuse
   it for a different claim. Read `carlina_status.work_delivery` for the key's
   saved response default and maximum; omit `max_response_bytes` to use the
   default or supply an override within the maximum. Choose settings for the
   actual client/tunnel, not a blanket 64 MiB assumption.
   Use `batch_id` when assigning a particular ready batch. A bounded long poll
   is preferable to rapid polling. Keep `wait_seconds` below the client's tool
   timeout with room for transport and database work; the current server caps
   the wait at 60 seconds. A 20-second wait fits a 120-second client timeout.
2. Execute the returned packet, including its metadata and continuity context.
   Follow the skill's [passive lease context and recovery](../SKILL.md#passive-lease-context-and-recovery)
   rule for standard-first claims, compact reuse, and same-lease refresh after
   model context loss.
   Follow `request.response_format`, which carries the schema once in the compact
   claim. Multi-page packets require every requested
   page exactly once in order, with separate page roots. Do not submit one page
   for an entire batch.
   Images arrive as native MCP image blocks corresponding to
   `carlina-mcp-image:N` placeholders. Seeing a path or base64 text alone is not
   visual inspection.
   Retrieve only missing deferred images; inline images and a null
   `delivery.next_offset` need no extra fetch. Follow every non-null offset.
   New page schemas require boolean `blank` on every page: `true` with empty
   roots after explicit visual judgment, `false` for other content. They reject
   contradictory content or undeclared empty output;
   omitted TOCs keep their separate source-TOC marker. Follow the frozen schema
   for older executions.
   Issue-aware v3 packets also require `issues` on every page. Read the
   [issue contract and web guidance](issues.md): use an empty array when no
   concrete concern was found, and retain uncertain readings as sidecar reports
   rather than editorial source prose. Web corroboration is optional, bounded,
   and permitted only under the caller's policy and actual tool availability.
3. Renew before lease expiry during long inference. The default lease is 300
   seconds; request an appropriate duration (30–3,600 seconds) and keep the
   orchestrator able to renew while its worker runs. Submit the assistant response
   under the active lease token, or release the lease with a concise reason.
   Retain the exact `work_item_id`, lease token, and response until acceptance is
   reconciled. An uncertain submission is not permission to author a replacement
   or reuse an expired lease; inspect the execution and follow the live retry
   contract. Treat lease tokens as transient credentials and keep them out of
   user-facing reports and committed files.
   The current submission schema accepts native JSON content directly, for
   example `response={"content":{"pages":[...]}}`. A text response can also use
   `response={"content":"<assistant output>"}` with JSON encoded **once** inside
   that string. Never double-encode or add a `format` field. Empty optional
   tool-call fields can be omitted. Acknowledgements do not repeat your content.
   `accepted` means the response contract and advertised packet-local preflight
   passed; it does not certify source grounding or final publication. Poll for
   product validation, checkpoint progress and retry feedback.
4. Continue until no work is ready, then call `carlina_job_get` for the same execution.
5. Repeat while the execution is active. Keep only one claimant per ready
   sequential batch. For concurrent plans, reconcile every batch in the parent
   before assuming the document is complete.
6. After finalization, list open and confirmed ledger issues and investigate them
   alongside the requested structural and visual review. Capture is a review aid,
   not a claim that every problem was detected.

`job_get` returns batch counts by default. Fetch individual batch records only
when needed with `include_batches=true`, `batch_offset`, and `batch_limit`.
Start/claim `include_details=true` is for diagnostics, not the ordinary loop.
Newer servers report `validation_error` with the failed batch, physical page,
stage, reason and attempt. A `validating` batch is awaiting product acceptance;
`completed` reflects validated progress. `unverified` means a terminal run lacks
enough retained validation evidence, and `blocked` can mean downstream work was
never attempted. Do not infer retries from `counts.total` alone.

Inspect `metadata_preparation` on document/job reads when preparation is unclear.
Its stage, status, reason, and requested/admitted/declared input-token diagnostics
distinguish required metadata failure from optional evidence limits. `limited`
or `provisional` preparation is not a terminal execution failure: follow the
actual execution state and continue ready work. Do not cancel, restart, or raise
the declared profile merely to eliminate optional uncertainty. Required-stage
errors, cancellation, and genuine execution failure are not provisional success.

If a described argument such as `dry_run` or `include_batches` is missing from
the actual tool schema, refresh the connection's tool catalog. A current server
and an older client catalog can coexist; repeatedly changing the completion
payload will not repair catalog drift.

New PDF runs supply PNG images at up to 2,000 pixels on the longest edge.
Existing executions retain their original image format for replay. If a ready
image claim is slow, inspect the client's attachment resizing/encoding time;
an empty long poll intentionally waits, but a ready packet should return early.
Do not repeatedly reclaim or restart a job to diagnose client image processing.

On rejection, use `validation_errors` paths to correct the existing payload;
`lease_active` tells you whether the same lease remains usable. Optional
`work_submit(dry_run=true)` checks that complete payload against the server's schema
and available packet preflight without recording or accepting it. Never submit
placeholders, partial pages or dummy prose to test transport. Do not substitute a hand-copied partial schema
or change the response wrapper to troubleshoot a nested field. If validation
still rejects the same corrected field, inspect that path before another retry.

To hand work back, call `carlina_work_release(work_item_id, lease_token, reason)`
using the claim's exact fields. This works for metadata, page and translation
leases. It becomes reclaimable immediately; do not sleep until expiry. A rejected
submission alone does not require release/reclaim or a fresh execution.

## Transcription and structural fidelity

Keep visible text in its source script, including diacritics, combining marks,
historical forms and meaningful punctuation. Do not ASCII-fold or transliterate
to make HTML/JSON easier to emit. Unicode text and equivalent HTML/JSON escapes
are valid; escape markup delimiters without double-encoding the content. A
visually similar Latin/Cyrillic letter is not interchangeable. Footnote-marker
superscript digits may be normalized under the packet's explicit rule; that is
not a rule for ordinary words, accents or formulas. Validators check structural
and catastrophic grounding errors, not every glyph: inspect source images for
uncertain readings and report unresolved fidelity limits.

For lists, preserve the printed ordinal and numbering style at each nesting
level. Use the packet's semantic `ol[start,type,reversed]` and `li[value]`
conventions; a new page or return from nested bullets does not imply item 1.
Each child list belongs inside its owning `li`; only direct items advance the
parent counter. Distinguish a continuing container with a new item from an
actually split item: only the latter receives the item continuation marker.
If a split item's ordinal is absent and cannot be established from supplied
evidence, do not invent it. Preserve intentional jumps/restarts and compound
printed labels using the packet's supported representation.

When the packet advertises `ruby-vocabulary-v1`, keep printed readings
(furigana and similar) as `<ruby>base<rt>reading</rt></ruby>` rather than
flattening them into the text or into brackets. Review search and
`replace_text` see only the base text, so fix a wrong reading with
`replacement_html`. When it advertises
`column-aligned-v1`, a table row continued from the previous page keeps one cell
per column, with empty cells where a column has nothing on this page, so each
parallel stream stays in its own column. Do not add spaces between characters of
Japanese, Chinese or Thai text to mark a page join; assembly chooses the
separator.

Use editable Mermaid diagrams only when the leased instructions advertise
`mermaid-diagram-v1`. Follow that packet's exact grammar and limits: the literal
template contains stable node IDs and label placeholders, while source-language
labels live in ordinary paragraphs bound by IDs. Never put translated wording
directly into syntax, add raw SVG/configuration/click handlers, or invent nodes
and connections. Unsupported, complex or uncertain diagrams use the packet's
image-crop path. Older frozen packets retain their image instructions; do not
add guessed settings to upgrade them. The newest backend renders this restricted
flowchart format to SVG for previews and exports. Verify the generated result;
successful template validation alone does not establish layout or glyph quality.

If `waiting_review` appears after extraction begins, inspect its supplied UI path
and review details. Complete image-coordinate review when that is within the
user's authorization and available capabilities; otherwise provide the precise
handoff. Resume the same execution after review.

## Finish

Verify terminal success and completed coverage of every selected physical page.
Missing retained HTML page markers alone do not prove failed extraction: blank
pages or excluded contents pages may lack markers. Preserve metadata, headings,
tables, figures/captions, notes, and ordering.

Report provisional heading preparation when present. Use bounded source checks
to investigate consequential heading uncertainty; do not present limited
optional evidence as a complete heading review.

Use the bounded [review workflow](review.md) for quality checks. Retrieve the whole
HTML with `carlina_document_get(include_output=true)` only when the full artifact
is needed (`output_format="markdown"` for Markdown). After completion, read
`assembly_check` in `carlina_job_get`: advisories name lost inline markup,
ambiguous table continuations and page-join spaces to check against the scans. Successful processing does not certify transcription or
export quality.

## Recover the existing execution

After a disconnect or worker failure, call `carlina_job_get` for the exact saved
`execution_id`. An empty claim, lost submit response, or child-agent timeout is
not evidence that the execution failed. Distinguish these server states:

- **Active:** continue the existing claim/renew/submit/poll flow. Reconcile an
  uncertain submission using its existing identity; do not regenerate completed
  work. Accepted output still needs product validation before it counts as a
  validated page.
- **Delivery pending:** if `product.delivery.pending` is true, follow its
  `next_action` and poll the same execution. Polling reconciles the saved initial
  or resume queue intent. Do not create another execution to repair dispatch.
- **Suspended:** read `product.recovery`, including `resumable`, `reason`,
  `suspension_reason`, `completed_work_items`, `checkpoint_pages`, and the returned
  next call. Suspension preserves accepted responses and checkpoints but
  invalidates leases; claims and submissions cannot proceed until resumed.
- **Terminal failed/cancelled:** `carlina_job_resume` cannot reopen it. Historical
  executions without recovery proofs also remain terminal. Inspect the document
  and its checkpoint before deciding whether an explicitly intended fresh run is
  appropriate; do not reinterpret a request to resume as permission to discard it.

For `assembly_repair_required` or a terminal list-continuation style/ordinal
conflict, follow `product.assembly_recovery` or call
`carlina_document_recover(execution_id=..., operation="inspect")`. Fetch only the
identified pages and compare list hierarchy with the source scans. Preserve odd
printed labels and independent lists; do not renumber them to satisfy assembly.
Preview with `repairs=[{page_number, expected_sha256, html}]` and the returned
`snapshot_revision` as `expected_revision`. Apply the identical payload with its
preview token and a stable idempotency key, then poll the returned new execution
and verify the completed document revision. The original execution stays
historical and its accepted responses remain immutable. Image-bearing pages,
missing accepted batches, changed source or later executions require a separate
recovery decision; never bypass those fences with database edits.

When suspended and `resumable` is true, call
`carlina_job_resume(execution_id=..., idempotency_key=...)`. Keep one stable key
for that resume attempt and repeat the same arguments/key after an uncertain
response. A later suspension needs a new key: replaying an old key returns its
historical receipt, not a new resume. Keep the same execution ID, poll it, then
claim fresh work normally. Do not reuse an invalidated lease or resubmit accepted
responses; the server replays retained responses against their exact packets.

Missing/changed source files, changed settings, conflicting execution bindings,
or `recovery_replay_conflict` block recovery without deleting progress. Report
the specific blocker and affected batch/page when supplied. Do not bypass it by
editing settings/database state, replacing source bytes, or issuing repeated
resume attempts against an unchanged blocker. If the same operational failure
recurs after a resume, inspect its new reason before deciding on another attempt;
report a persistent blocker instead of looping indefinitely.

Renderer startup retries are bounded by the server. The document card's Resume
uses the same recovery service, and pausing a new frozen passive extraction
suspends it. This PDF recovery tool is not a generic translation restart tool.
A new passive start does not resume a failed-run checkpoint;
`restart_from_scratch=true` explicitly permits discarding it. Cancel only the
exact execution when the work is intentionally abandoned. Enter the separate
review/translation workflows when processing is complete.
