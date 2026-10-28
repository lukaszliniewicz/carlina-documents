# Extraction cleanup and efficient passive workers

## Extraction issue ledger

New passive, issue-aware PDF batches carry a bounded `issues` array per page alongside
HTML and the explicit blank declaration. Reports retain the original excerpt,
reason, candidate readings and corroborating references, with a physical page
number. They do not insert editorial uncertainty into transcription. The new
`mcp_extraction_issues_policy=issues-v1` marker freezes this contract; retained
older packets retain their existing prompt/schema/fingerprint.

The ledger uses encrypted payloads and an encrypted extraction/resume sidecar.
Reports are imported with final output in the same transaction; replay does not
reset a review decision. It is owner-scoped, paginated and available through the
document preview's Review issues panel and four MCP operations: list, get,
report and update. Manual reports support already-extracted documents.
Managed extraction retains its existing HTML completion protocol; automatic
issue capture there is not enabled in this slice. Its completed documents can
still use the same manual/MCP review ledger.
Review states are open, confirmed, resolved and dismissed. Original reports
remain immutable; updates append notes with issue-version and current-source
revision fences plus idempotency. A changed source sets `context_stale`; it does
not silently resolve or rewrite the report. Resolving a ledger issue never
changes document text or an existing translation snapshot.

Native harness web tools may corroborate uncertain names using minimal public
queries when the caller permits it. Consulted references and plausible readings
belong in the report; web spelling never overrides a clear printed form.
Managed model calls do not gain web tooling through prompt guidance. The
OpenCode worker exposes websearch/webfetch, while its per-dispatch web policy is
a behavioral instruction, not a permission-system guarantee.

## Diagrams and list numbering

The semantic contract supports a `mermaid-diagram-v1` figure for legible
flowcharts. It supports rectangular, rounded and
diamond nodes, directed or undirected edges, and directed-edge labels. Unsupported
diagrams, uncertain connections and unreadable labels use the existing image crop
instead; extraction must never simplify the source merely to fit the vocabulary.
Diagram extraction follows the existing `extract_images` option.

```html
<figure class="mermaid-diagram-v1" id="diagram-p12-1">
  <pre class="mermaid-template"><code>flowchart TD
n1["{{start}}"]
n2{"{{ready}}"}
n1 -->|"{{next}}"| n2</code></pre>
  <div class="mermaid-labels">
    <p id="diagram-p12-1--start">Start</p>
    <p id="diagram-p12-1--ready">Ready?</p>
    <p id="diagram-p12-1--next">Next</p>
  </div>
</figure>
```

Only printed captions become `figcaption`. Every placeholder has exactly one
label and every label is used. Node IDs use `n` plus one to six digits to avoid
Mermaid reserved words; label keys remain stable ASCII identifiers. Mermaid code is literal evidence in Translator;
label paragraphs, captions and accessibility descriptions are translated. The shared parser rejects
undeclared nodes, duplicate bindings, extra syntax, directives, configuration,
styles and callbacks. Limits are 256 nodes/labels, 512 edges, 64 KiB of template,
4096 characters per label and 128 diagrams per document. Namespaced project
composition retains the figure-to-label relationship.

`carlina.core.semantic_diagrams.materialize_mermaid` returns validated Mermaid
code with translated label characters escaped as Mermaid entities. Derived
previews and exports use `carlina.core.diagram_rendering` to parse the validated
restricted flowchart grammar and compile it through host-owned Graphviz 14.1.2.
This is a renderer for the supported Mermaid subset, not a general Mermaid
runtime. Labels become escaped plain DOT strings; user configuration, HTML
labels, event bindings and external resources are unavailable. Output passes a
strict SVG primitive/attribute allowlist, dimension checks, and time/size limits.
The stored template and translatable labels stay unchanged. HTML and EPUB embed
SVG; DOCX carries native SVG (the tested Pandoc path supplies no PNG fallback).
The current PDF engine rasterizes the SVG. Document Processor preview failures
display the stored representation with an explicit warning; translation previews
and exports fail rather than quietly omit the diagram. Font coverage and layout
still require visual review.

Raw model-authored SVG is not a supported extraction representation. Page
validation rejects inline SVG and SVG data images before canonicalization can
discard their artwork. This is distinct from the validated SVG generated from
accepted Mermaid diagrams during preview/export.

### Proposed reconstruction review

The next diagram contract should retain source artwork and its page/bbox
provenance alongside a proposed reconstruction. Mermaid remains appropriate for
supported flowcharts; bounded SVG primitives can preserve geometric diagrams
whose layout the Mermaid subset cannot express. Default extraction should keep
both representations for comparison, with the source selected until review.
An explicit vector-only instruction may omit the crop from the extracted edition;
it must retain the PDF and provenance. Selecting a reconstruction must not delete
the retained original.

Model-authored SVG needs its own validated semantic representation, rather than
an exception for arbitrary inline SVG. Keep geometry and stable label bindings
separate from translatable text, as Mermaid already does. Admit only inert local
primitives with bounded dimensions and complexity; reject executable content,
external resources, foreign HTML and unsupported styling. Review should compare
the original and reconstruction, including label accuracy and relationships,
then persist a revision-fenced choice shared by preview and all exports. SVG-only
output still needs source comparison and accessible text. This proposal is not
implemented; current extraction continues to use the accepted Mermaid/crop rule.

New extraction instructions require explicit `ol[start,type]` and `li[value]`
for visible ordinals, including page starts and returns from nested bullets.
Each nested list belongs inside its owning item. A split item's missing printed
marker is not permission to invent an ordinal. Complex compound legal labels
that HTML counters cannot represent remain literal paragraph text. Page
validation rejects malformed nesting, orphan items and invalid numeric attributes;
it does not prove that a valid number matches the scan.

The page merger preserves an incoming ordered-list start before discarding its
wrapper, counts direct items only and preserves reversed numbering. Conflicting
split-item ordinals or list styles fail explicitly. A continuation without an
explicit start retains its historical incrementing behavior. These checks
protect structural fidelity; they cannot detect a model confidently reading a
printed 7 as 1. Source-page review remains necessary for that case.

Split list items merge explicitly continuing child lists recursively; independent
child lists and repeated printed labels remain distinct. Join failures identify
the two physical pages and section without exposing extracted text. Packet
preflight and worker validation exercise this same join within the batch and
against an explicitly supplied adjacent preceding-page boundary.

Continued tables preserve physical page markers when repeated headers, captions
or column decorations are discarded. Relocated markers precede incoming row text,
including a split row; markers inside retained rows stay in place unless their
header is removed.
A header-only fragment appends its boundary to the last existing cell, or to an
existing caption when no cells exist. With neither, it keeps the marker before
the empty table. Assembly does not invent rows or captions. Page-scoped review
and EPUB page-list links consume these same markers. This assembly repair does
not rewrite completed documents or immutable Translator source plans.

An assembly list conflict preserves the complete checkpoint and suspends new
passive runs for repair. `carlina_document_recover` can also recover an eligible
historical failed run with frozen source proof and every accepted page batch.
Inspect and bounded fetch expose retained evidence; preview validates a topology
repair that preserves text, attributes, list styles and effective ordinals.
Apply requires the snapshot revision, exact preview token and idempotency key.
It publishes a separate completed execution, retains the original execution and
accepted responses, imports issue sidecars and stores an encrypted repair audit
in the same transaction. Changed source, newer execution, incomplete evidence,
unresolved semantics, image-bearing pages, duplicate work units from product
retries and evaluator-finalized batches block this bounded recovery path.
It performs no new extraction or provider calls.

New passive executions freeze `mcp_semantic_structure_policy=lists-diagrams-v1`.
Older passive executions omit the additional instructions so retained request
fingerprints do not change. Managed extraction and prompt previews use the new
instructions. Existing documents are not rewritten automatically.

New passive executions separately freeze
`mcp_image_accessibility_policy=image-descriptions-v1`. Managed extraction and
prompt previews use the current image guidance; retained passive executions
without that marker keep their exact original extraction/evaluation prompts and
cache identity. Review continuations retain the frozen marker or its absence.
The policy participates in prompt-cache identity and execution fingerprints;
queued instructions and completed documents are not rewritten.

The new instructions choose alternatives by purpose and surrounding context,
keep source captions separate from generated accessibility text, and require
visible, linked, structured descriptions for complex cropped diagrams/charts.
Editable flowcharts can carry a translatable short alternative and context
description. Export supplies a complete deterministic topology description in
addition. Unreadable labels or uncertain relationships remain crop/issue-ledger
cases; the model must not guess missing data to make a description sound complete.
Evaluation reviews these source-grounded adaptations rather than deleting them
as unprinted prose. It still verifies transcription against the page and never
marks generated descriptions as human reviewed. See the
[description contract](epub-export.md#structure-and-accessibility).

PDF image extraction and completed-document crop edits materialize the requested
bounding box with 0 px of added padding. Printed captions remain separate semantic
text; neighboring caption or frame pixels are excluded by the supplied box.
Retained original images allow later crop edits, and a new crop or extraction
materializes the current behavior for an existing document.

Unicode text is preserved through page normalization, assembly, UTF-8 semantic
storage and EPUB serialization. Tests cover accented Latin, decomposed combining
marks, Greek, Cyrillic, pointed Hebrew, Arabic, CJK, historical forms and ligatures.
Grounding checks normalize text for comparison only, not storage. The existing
footnote-marker contract deliberately converts numeric superscript glyphs to
ordinary digits inside the semantic superscript reference; it is not a general
ASCII conversion. New extraction instructions explicitly forbid transliteration,
accent stripping and spelling modernization. These tests establish deterministic
preservation, not an OCR accuracy score for real scans.

Project namespacing shortens a diagram's ID family together. EPUB reserves a
stable ASCII family for each diagram and rewrites local links, accessibility
references and other supported ID references with the same map. This prevents
Unicode or long figure IDs from losing their label bindings during export.

### Source-to-translation numbering synchronization

Current extraction review can correct numbering with revision-fenced scoped HTML
replacement and encrypted undo. Existing Translator snapshots and old exports
remain unchanged. Creating a new source plan from corrected bytes changes the
hash-derived block identities, so it is not a suitable numbering-only repair.

The explicit, one-way **numbering correction overlay** is stored encrypted per
translation project and applied to its full source preview and target
render/export projection. Original snapshots, target text, alignments and
translation revisions stay immutable. The overlay may alter only `ol[start,type,reversed]`
and `li[value]`; wording, item order, nesting, list kind, IDs and diagram topology
are outside this operation.

Preview compares three witnesses: the exact pinned source, its corrected
Document Processor revision, and the current translation/overlay revision. Bind
the owner, project, document, source-plan hash, selected element identities and
old/new attribute values. It admits only uniquely matched, unchanged list subtrees
apart from permitted attributes, with unchanged item text, topology and protected
references. Never use fuzzy wording matches or positional guesses after edits.
Ambiguous matches, changed text/nesting and stale revisions require review.
The comparison accounts for existing overlay values, so a later source correction
can explicitly replace a previous override or restore the pinned value.

The owner UI and MCP expose preview, selected apply, and undo. Apply holds the
source-plan writer fence, rejects active operations, rechecks the comparison,
and stores an idempotent revisioned receipt. Export requests pin that revision;
workers reject stale deliveries. Regenerated exports receive the new projection;
already downloaded files do not change. Target reconstruction and validation
finish before numbering attributes are projected, preserving source offsets.
The compact block editor retains the pinned representation; the dedicated full
source preview shows effective numbering. No background propagation occurs.
A nonempty overlay blocks source-plan replacement. Empty overlays permit a new
plan while retaining their monotonic revision; history cannot restore attributes
from an older plan. History is bounded to 100 events and 128 receipts with an
explicit full-history error rather than silent loss of retry or undo evidence.
Apply reserves the count and byte capacity needed to undo every outstanding
change, so these bounds cannot strand a nonempty overlay.

Clients retain global choices and concurrency limits, assign one execution owner
per book, and bound metadata/page/translation model work. Different books may
progress in parallel while list-heavy books use sequential page batches.
These client conventions supplement server owner, lease and revision fences.
## Remove marked running matter after extraction

The completed-document preview has **Remove headers / footers…** alongside
its editing controls. Select headers, footers, or both, optionally restrict
physical PDF page numbers, then preview. The confirmation button names the
number of elements to remove; the preview includes bounded examples and a
count of protected elements that will be kept. **Undo this cleanup** restores
the preceding revision while the panel remains open. Encrypted correction
history also supports later undo through the existing MCP review-history tools.

The shared service removes only elements explicitly classified with
`page_header` or `page_footer` classes. It does not guess from repeated text,
position, generic `<header>` tags, headings, or note content. Images, page
anchors, note references, note structures and referenced targets are protected.
A header that contains protected structure is kept and reported. Page-scoped
cleanup requires a known physical page association for the entire element.
Printed page labels may disappear if they are part of a marked footer, but
structural page anchors are retained. Misclassified ordinary paragraphs require
review corrections, not a more aggressive heuristic.

MCP uses `carlina_document_cleanup` with `dry_run=true` by default:

```json
{"document_id":"…","roles":["header","footer"],"page_numbers":[1,2,3],"dry_run":true}
```

Save with the same scope, the preview's `revision` as `expected_revision`, a
stable `idempotency_key`, and `dry_run=false`. Omit `page_numbers` for the whole
document. The editor and MCP share owner isolation, semantic validation,
optimistic revision fencing, atomic publication, encrypted inverse deltas and
idempotent retries. Existing correction-history retention bounds still apply;
if a very large inverse delta cannot fit, split the selection into page ranges.
A synthetic 649-page / 1,298-element cleanup fits and round-trips exactly.

Current HTML, Markdown and knowledge-source output are synchronized. Existing
export files are not rewritten, and existing Translator source snapshots are
not changed. Regenerate an export or deliberately prepare a new translation
snapshot after accepting a cleanup. Adding this feature does not clean any
existing user document automatically.

## Unicode identifiers and edit validation

Generated heading IDs retain Unicode alphanumeric characters, so the validator
must accept the same alphabet on subsequent edits. Identifier values and local
fragment references use a shared bounded Unicode-alphanumeric rule; CSS class
and policy tokens remain ASCII-only. Existing identifiers are neither renamed
nor normalized. Document Processor, Translator rendering, and the host export
safety guard agree on these references. Unsafe markup and invalid references
still block edits, now with bounded diagnostic codes rather than source text in
the error message.

The post167 regression fixes a completion/edit mismatch: the first pass generated
accented IDs after attribute validation, while the second pass removed and
regenerated those same IDs and raised `invalid_attribute_removed`. Stored hashes
and empty completion diagnostics therefore did not prove editability. Tests now
assert diagnostic as well as byte-level idempotence across multiple scripts, and
the 649-page cleanup fixture uses generated accented heading IDs. Real-document
acceptance uses read-only source acquisition and an isolated in-memory database;
no production content is edited by this acceptance.

## Selectable worker transport

`carlina_work_claim` and `carlina_work_get` accept `response_mode`:

| Mode | Intended use |
| --- | --- |
| `standard` | Default compatible worker packet, including instructions, current evidence, output schema and native images. |
| `compact` | Omits system/developer messages, generation parameters and repeated submission guidance, with hashes and retrieval paths. Current user evidence, images and output schema remain. |
| `minimal` | Lease identity, workload/batch identity and omission references, without task messages or images. Not sufficient to perform extraction alone. |
| `full` | Full diagnostic envelope and frozen request. `include_details=true` on claim remains the compatibility alias. |

`omit_fields` adds explicit omissions: `system_prompt`, `messages`, `images`,
`response_format`, `parameters`, `tools`, `submission_contract`. These are
transport projections only. Stored requests, output schemas, page identities,
replay fingerprints and validation rules do not change.

A fresh worker needs `standard`, even if its parent has already read the
instructions. Use `compact` only when the worker has read the omitted content
and its hash matches. An omission is never permission to ignore a constraint.
Large image bytes are removed before text transport; native images are emitted
only when requested, without an unnecessary extra deep copy of the packet.

Use `carlina_work_get` with the current `work_item_id` and `lease_token` to
recover omitted content. It does not claim, increment attempts, or renew the
lease, and rejects a foreign owner, ended execution or stale/expired lease.
An optional `path` is a JSON pointer inside `/request`. For example:

```json
{"work_item_id":"…","lease_token":"…","path":"/request/messages/0","offset":0,"max_chars":12000}
```

Large text/JSON fields return an explicit slice, `next_offset`, lengths and
hashes. `sha256` identifies the original canonical JSON value;
`text_sha256` verifies the paginated textual representation. Container reads
replace image blocks with references rather than filling slices with base64.
Fetch an individual image-block path for its native image. Follow all required
slices before submitting; never treat a truncated slice as the whole prompt.

## Scheduling and local work

### Delivery bytes and recoverable claims

Model input-token admission and tool-response transport are separate budgets.
`work_claim` and `work_get` use the authenticated access key's saved response
default when `max_response_bytes` is omitted. Each key also has a maximum;
explicit overrides above it are rejected before acquiring work. Existing keys
retain an 8 MiB default and 64 MiB maximum. Both values are configurable within
64 KiB–64 MiB, with the default no larger than the maximum. `carlina_status` reports
the effective values in `work_delivery`. Use distinct keys for direct and tunnel
routes with different limits. These are conservative transport bounds, not a
claim about any connector's undocumented capacity. Budget checks include native images,
both textual/structured projections, UTF-8 encoding, and JSON-RPC envelope headroom.
Small packets remain inline. Oversized packets return `delivery.mode=deferred`
with lossless references; exceptionally large text also falls back to a small
receipt with a paginated `/request` read. Frozen instructions, output schema,
model-context admission, page selection, image resolution and bytes do not change.

For deferred images only, after reading the text/schema, call `carlina_work_get` with the receipt's work ID
and lease token, `image_offset=0`, and `image_limit=1` (up to four). The returned
native image blocks retain their request order and original JSON Pointer paths.
The server returns the largest contiguous prefix that fits the byte budget;
follow `delivery.next_offset` until it is null. An image that cannot fit alone
returns `work_image_too_large` with the needed byte size or a lower bound; it is never silently
resized or skipped. Increase the budget only when the client can receive it and
the key's configured maximum permits it. Do not refetch images already inline
when no evidence is missing and `delivery.next_offset` is null.
Existing individual image `path` reads remain available. All required metadata
evidence must be inspected before the one complete metadata answer is submitted;
transport pagination is not partial metadata completion or a smaller context window.

The operator can configure an existing key without rotating its bearer:

```bash
flask --app iarf_assistant.wsgi mcp configure-transport \
  --user-id 1 --key-id KEY_ID \
  --response-default-bytes 8388608 --response-max-bytes 8388608
```

Run this through the intended runtime and data-root configuration. This example
caps a tunnel key at 8 MiB; it is not an instruction to alter the managed instance.
`issue-key` accepts the same two options; `list-keys` includes both values.
The setting controls passive-work delivery, not model input/output token budgets
or arbitrary exports. Runtime activation requires the additive host migration;
source changes alone do not reconfigure an existing installation.

Supply a unique stable `claim_request_id` for each intended claim. Retry the same
ID, authenticated key, execution, worker and work-selection/lease arguments after
an uncertain response. Changing response mode, omissions or byte budget is allowed.
The successful claim and its receipt are atomic. A retry returns the same active
lease (including any renewed expiry) without extending it or incrementing attempts.
Empty polls do not reserve the ID. A conflicting reuse returns
`claim_request_conflict`; an ended lease returns `claim_request_closed`, never a
different packet. Inspect the execution before using a new ID to acquire new work.
Receipts contain identity/fingerprint/attempt metadata, not prompts or lease tokens.
Legacy callers without an ID retain their existing acquisition semantics.

Projection failures are caught after leasing and return redacted
`work_delivery_failed` guidance. Logs record correlation identity/error class,
and automatic deferral logs byte counts, never document content or lease tokens.
These logs establish local preparation, not delivery acknowledgement by a client.
Retrying the same claim ID with `response_mode=minimal`, then fetching bounded
text/image batches, recovers from a lost or oversized response without restarting
the document or waiting for lease expiry.

Sequential extraction remains sequential. A completed multipage batch with a
valid saved boundary is no longer reprocessed on every resume, and batched
extraction does not rebuild irrelevant previous-page histories. Missing or
partial checkpoints still take the full processing/validation path.

New waiting checkpoints retain all outstanding work IDs in the current window.
An idle queue poll selects only their indexed identity/state columns, not
request/response bodies, and avoids reopening the PDF, encoding images, or
replaying HTML until something changes. Any completion in the window wakes
normal processing. Cancellation/pause, changed execution or queue binding,
missing work and invalid checkpoints bypass this fast path. The existing
five-second retry cadence remains; this optimization removes work done during
those retries, not the durability checks themselves.

Concurrent batch plans accept optional `max_in_flight` from 1 to 16; the runtime
default is 4. Only that bounded window is rendered/published while it is waiting
for external workers. Unscheduled ranges remain in the durable plan, not in
memory-only scheduler state. Legacy plan normalization does not acquire new
fields unless explicitly supplied, preserving existing request fingerprints.
Changing the local process-pool size is separate and is not part of this pass.

Use `wait_seconds` on work claims instead of a fixed 45-second sleep between
batches. `carlina_job_get` remains product truth: accepted transport output is
not proof of successful semantic validation. Its `include_details=true` view
adds recorded local `packet_preparation` and `product_validation` calls, total
seconds and maximum seconds when available. Model inference is not included in
these local phase counters. Server checkpoints and paginated batch status make
runs resumable without a separate authoritative progress file.

Batch page ranges remain the caller's explicit decision. Do not increase dense
German batches merely because there is unused input context: output tokens,
notes, tables, images and semantic overhead can be limiting. A larger or
balanced final batch is appropriate only within the confirmed output budget.
This release does not silently replace existing plans or widen concurrency.

## Worker template and evidence

### Explicit blank pages and submission preflight

New passive PDF runs freeze `mcp_page_blank_policy=agent-declared-v1` and emit
the `document_page_batch_v2` schema. Every page has a required boolean `blank`.
The image-reading agent sets it true only when no substantive content is visible,
with empty `main_content` and `footnotes` roots. Carlina checks consistency; it
does not infer blankness from pixel density or trust noisy OCR over that explicit
visual judgment. Omitted printed TOCs retain their distinct omission marker and
`blank=false`. The accepted completion retains the declaration and page identity.
Existing executions without the marker retain their original schema and prompt.

The Document Processor preflight hook checks the new leased page packet's order,
page roots, semantic markup, declaration and explicit list continuity before accepting a submission. A
failure returns bounded field diagnostics while the same lease remains available
for correction. A dry run performs the same checks without recording acceptance.
This pure preflight neither loads source files nor validates source grounding;
the worker remains responsible for full validation and atomic publication.

Review diagnostics use the document's shallowest body heading as the root level.
Note reference locations and common heading context are derived from incoming
references, without inventing a source page for a collected note. Unreferenced
notes receive a neutral status; broken targets and duplicate IDs remain errors.
The dedicated `carlina_document_repair_note_reference` operation restores one
source-verified missing marker with revision checks, preview and encrypted undo.
Generic HTML edits continue to protect all existing note references.

### Local note sequences and relationship repair

The source resolver now treats visible note labels and `fmX`/`fX` shorthand as
local evidence. A unique same-page match can resolve an unscoped marker; an
explicit `data-footnote-scope` permits a unique match within a witnessed chapter
or subsection sequence, including notes on later pages. A scope is an opaque
token, not a heading title or a guessed number. Repeated labels and identical
note text retain distinct note IDs. Nearest-page, last-used-label and unrelated
unused-note fallbacks are removed. Ambiguous markers remain visible and unlinked.
Bare superscripts remain candidates, since an exponent is not necessarily a note.
New default extraction guidance preserves true chapter endnotes as note bodies
and permits witnessed scope tokens; already issued work packets stay unchanged.

New runs also return bounded `note_arrangement` metadata: source `placement`
(`page_footnotes`, `chapter_endnotes`, `document_endnotes`, `mixed`, `none`, or
`unknown`), `numbering` scope (`document`, `chapter`, `section`, `page`, `mixed`,
or `unknown`), the witnessed `scope_heading_level` or null, up to 32 supplied
`source_pages`, and at most 800 characters of visual evidence. Placement and
numbering are independent: document-end notes may restart by chapter. A sampled
label of 1 cannot establish a restart, and a sample without notes cannot establish
their absence. The extension persists in canonical document settings and travels
in the bounded Translator source snapshot; existing metadata columns need no
migration.

New full-note instructions use unique opaque marker/body IDs, explicit local
links only for known relationships, and separate printed labels. Repeated labels,
letters, symbols and punctuation survive matching without becoming global IDs
or an invented numbering sequence. Superscript digits retain the established
ordinary-digit presentation rule. The new passive marker
`mcp_note_arrangement_policy=source-arrangement-v1` freezes both the metadata
schema and note instructions. Retained runs without it keep their previous
request bytes, including runs with the earlier native-vision policy marker.

EPUB export places linked local notes after their governing chapter or section
when the arrangement establishes that heading level and each note's incoming
links identify one unit. A long chapter's notes follow its final fragment.
Explicit scope conflicts, references from multiple units, unreferenced notes,
and uncertain boundaries retain their source position. Citation attributes
participate in ownership, and inherited scope is retained when a body moves.
Wrappers containing the end of one chapter and the next opener retain their text
order around the inserted notes. Native list counters
retain their source values; notes with printed markers use lists without a
second visible counter. This is an EPUB projection: saved documents, translation
snapshots and other export formats retain their positions and identities.

`carlina_document_review_note_references` provides paginated existing references
and bare superscript candidates, with revision-bound `element_id`, zero-based
`marker_index`, label, href, page numbers and heading context. Keep the same
`expected_revision` while paging. Use the existing note inventory, source search,
fetch and physical page tools to establish the actual destination; shared labels
or multiple references alone are not evidence of a wrong link.

`carlina_document_repair_note_topology` accepts 1–50 explicit operations:

- `promote_note`: `element_id`, `label`, `scope_id`; classify one witnessed leaf
  list item, paragraph or aside as a semantic note, without moving its prose.
- `set_note_scope`: `element_id`, `scope_id`; annotate an existing semantic note.
- `link_reference`: `element_id`, `marker_index`, `note_element_id`,
  `expected_label`, `expected_href`; link or retarget that exact marker. Use JSON
  null for the href of a bare superscript. A body promoted in the same batch is
  a valid destination. Existing backlinks to a retargeted reference follow it.

Every operation includes its `action` name. Scope tokens contain 1–128 ASCII
letters, digits or `._:-`; visible text remains Unicode. The default dry run
returns the exact proposed edits. Save with the reviewed `expected_revision`, a
stable `idempotency_key` and `dry_run=false`. A batch uses the existing atomic
publication, encrypted undo and owner boundary. Generic edit guards are unchanged.
Stored semantic errors must be diagnosed separately; this repair lane requires
a valid current semantic document and rejects an invalid successor.

Relationship repair does not modify existing
Translator source plans, translated text, or previous exports. Relationship repair
for frozen translation plans is a separate follow-up; list-numbering overlays do
not change note destinations. Affected translated passages may also require
targeted review if their earlier context included the wrong note.

### Worker practice

Workers use native structured content, schema-derived page counts, semantic text
children, complete submissions and actual lease expiry. Send a normal response
once; a complete-payload dry run is optional. Avoid partial-payload probes.


## Metadata preparation performance

The metadata/structure path prepares its initial image window before
issuing the primary request. An expanded TOC request and heading requests depend
on earlier results. Page-batch windows also render their missing images before
dispatching concurrent external work. One backend queue worker can leave several
books waiting for external agents, so the per-book packet window is not a global
CPU or memory limit.

For new budgeted passive metadata runs, an unchanged wait now reads only indexed
work identity/state columns before opening the parser. Pending or leased
non-batch work for the exact active execution suspends the job again; completion,
cancellation, missing work, queue controls or changed ownership/state wake the
normal path. Page-batch checkpoints retain their existing wake-on-any-completion
behavior. This prevents preparation on empty metadata polls; it does not cache
images across completed metadata stages. Rendering concurrency is a separate
mechanism described below.

Both [PyMuPDF](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html)
and [PDFium](https://pypdfium2.readthedocs.io/en/stable/python_api.html#incompatibility-with-threading)
require process isolation for concurrent rendering. PDFium therefore provides no
threading simplification. MuPDF's standalone `mutool draw` also offers banded
rendering with native threads; that path has not been benchmarked here.

Managed parsers now share a process quota under the host-owned Document Processor
data directory. Each child opens its own document and calls MuPDF on its main
thread. Child-held OS file locks enforce the quota across book jobs and release
it on exit. Linux children arm an OS parent-death signal, with a parent-identity
check closing the startup race. Other platforms use control-pipe EOF; a native
call holding the GIL can delay that fallback until it returns. Admission is
cancellable, startup and page work have deadlines, and a failed worker produces
an error instead of falling back to uncapped rendering. A conflicting worker
limit is rejected while any old slot remains active.

Pre-ready child exits receive at most two additional startup attempts within the
original deadline and quota. Exhausted errors retain phase and exit-code
diagnostics. Stderr is drained with bounded memory; only allowlisted exception
classes and content-free counts are exposed. Arbitrary child output is never
logged. New passive PDF runs suspend after recoverable renderer failures and
can resume through the same execution; see the
[MCP recovery contract](mcp-integration-architecture.md).

Metadata and heading evidence images are rendered and encoded in bounded chunks.
Workers pass file receipts rather than image blobs; they exit before the parser
consumes the chunk or waits for a model. Unconsumed files are removed when the
consumer exits. Contact-sheet thumbnail preparation remains serial and has its
own timer. Page-batch rendering uses the same quota; external model packet limits
remain separate. Standalone parsers without a host data root retain serial
rendering. The existing image policy and encoded bytes are preserved.
An abrupt parent kill bypasses the parent's temporary-directory cleanup; staged
files can remain under the output
directory. Automatic orphan-file reclamation is deferred. Ordinary exceptions,
pause/stop and early consumer exit clean unconsumed files.

This limits active render processes and queued pages, not bytes or total RSS:
a single unusually large page can still use substantial memory. Access is
opportunistic between chunks, with no strict fairness guarantee. Alternative
renderers must preserve crop/rotation/annotation behavior and text/image fidelity,
not merely improve elapsed time.

### Runtime timing counters

`preparation_timings` contains bounded numeric aggregates for metadata, heading
analysis, text-layer sampling, heading candidates, contact sheets, image
preparation and completion dispatch. Each stage records calls and elapsed time;
method timers also record current-thread CPU time. Image chunks report worker
render/encode time sums, startup and quota wait, image bytes, and sampled peak
aggregate RSS of that chunk's children. RSS sampling is an observation, not a
memory cap, and interrupted chunks can undercount unfinished work. Parent CPU
time excludes child CPU. Nested stage times overlap and must not be added to
derive job duration. Completion dispatch can include provider waiting.

The worker retains sanitized preparation timings when metadata yields or fails,
using the active owner/document/job/execution fence. It updates only the timing
subtree, preserving current settings and controls. Document and job projections
expose these counters without prompts, images or extracted text. Job queries
require `include_details: true` for preparation counters, keeping routine polls
compact; the small external-work summary is also present on compact polls.

Exact execution projections also expose `external_work_timing`: pending, leased
and completed counts, the age of the oldest never-claimed pending item, and the
sum/count of completed items' latest-lease-to-completion intervals. Those
intervals include agent/tool/transport time and are not inference measurements;
earlier attempts cannot be reconstructed from the current lease timestamp.
The query reads only identity, state and timestamp columns. Above 5,000 items it
reports `truncated: true` and null aggregates rather than misleading totals.
Historical execution queries retain their own external-work measurements rather
than inheriting a later run's preparation counters.

The current adaptive scale helper also references `page.doc`, which is absent
in the installed PyMuPDF version (`page.parent` exists). Its broad exception
handler silently skips image-resolution accounting. Correcting that behavior,
changing encoding settings, or replacing the renderer can change image bytes
and therefore passive request fingerprints. Preserve retained executions' image
policy and introduce any changed rendering behavior through an explicit new-run
policy, with output-quality acceptance.
