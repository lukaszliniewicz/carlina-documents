# Extraction cleanup and efficient passive workers

## Reviewing and correcting a document

Open a completed document and choose **Correct text** to proofread its extraction.
The editor shows 30 blocks at a time. Use Previous or Next, or the text-block
number, to move through a book. Corrections stay in the editor when you change
pages. Words inside links or formatted passages have their own fields, so
proofreading never requires editing HTML. Lists, tables, figures, note links,
identifiers and element order are protected. Structural repairs are separate
actions.

**Compare PDF** shows the source beside the extraction, with page navigation,
zoom and a download of the original file. Carlina renders pages one at a time, so
the browser does not need an embedded PDF viewer. Crop review has a page-number
jump and optional percentage coordinates. Image-crop coordinates use a 0–1000
scale. You can also move or resize boxes with the arrow keys, using Shift for
larger steps.

Closing a review that has changes asks before discarding corrections. Page
reloads and navigation also warn about unsaved work, and saves in progress block
dismissal of the dialog. If a save conflicts with a newer document, the local
correction stays available instead of overwriting it. Copy any corrections you
need before you deliberately reload the current version. This protects the
current editing session only. It is not permanent draft storage or a document
version history.

## Extraction issue ledger

New passive, issue-aware PDF batches carry a bounded `issues` array for each page,
alongside the HTML and the explicit blank declaration. Each report keeps the
original excerpt, the reason, candidate readings and corroborating references,
with a physical page number. Reports do not insert editorial uncertainty into the
transcription. The new `mcp_extraction_issues_policy=issues-v1` marker freezes
this contract. Retained older packets keep their existing prompt, schema and
fingerprint.

The ledger uses encrypted payloads and an encrypted sidecar for extraction and
resume. Reports are imported with the final output in the same transaction, and
replaying an import does not reset a review decision. The ledger is owner-scoped
and paginated. It is available through the Review issues panel in the document
preview and through four MCP operations: list, get, report and update. Manual
reports support already-extracted documents. Managed extraction keeps its
existing HTML completion protocol, and automatic issue capture is not enabled
there in this slice. Its completed documents can still use the manual and MCP
review ledger.

Review states are open, confirmed, resolved and dismissed. Original reports are
immutable. Updates append notes, with issue-version and current-source-revision
fences plus idempotency. A changed source sets `context_stale`, and it does not
silently resolve or rewrite the report. Resolving a ledger issue never changes
document text or an existing translation snapshot.

When the caller permits it, native harness web tools may corroborate uncertain
names with minimal public queries. Consulted references and plausible readings
belong in the report. Web spelling never overrides a clear printed form. Prompt
guidance does not give managed model calls web tooling. The OpenCode worker
exposes websearch and webfetch, but its per-dispatch web policy is a behavioural
instruction, not a permission-system guarantee.

## Diagrams and list numbering

The semantic contract supports a `mermaid-diagram-v1` figure for legible
flowcharts. It supports rectangular, rounded and diamond nodes, directed or
undirected edges, and labels on directed edges. When a diagram is unsupported, a
connection is uncertain or a label is unreadable, extraction uses the existing
image crop instead. It must never simplify the source merely to fit the
vocabulary. Diagram extraction follows the existing `extract_images` option.

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
label, and every label is used. Node IDs use `n` plus one to six digits, which
avoids Mermaid reserved words. Label keys remain stable ASCII identifiers.
Translator treats Mermaid code as literal evidence and translates label
paragraphs, captions and accessibility descriptions. The shared parser rejects
undeclared nodes, duplicate bindings, extra syntax, directives, configuration,
styles and callbacks. The limits are 256 nodes and labels, 512 edges, 64 KiB of
template, 4096 characters per label and 128 diagrams per document. Namespaced
project composition keeps the figure-to-label relationship.

`carlina.core.semantic_diagrams.materialize_mermaid` returns validated Mermaid
code with translated label characters escaped as Mermaid entities. Derived
previews and exports use `carlina.core.diagram_rendering`. It parses the
validated restricted flowchart grammar and compiles it through host-owned
Graphviz 14.1.2. This renders the supported Mermaid subset only. It is not a
general Mermaid runtime. Labels become escaped plain DOT strings. User
configuration, HTML labels, event bindings and external resources are
unavailable. Output passes a strict allowlist of SVG primitives and attributes,
dimension checks, and time and size limits. The stored template and translatable
labels stay unchanged. HTML and EPUB embed the SVG. DOCX carries native SVG (the
tested Pandoc path supplies no PNG fallback). The current PDF engine rasterises
the SVG. When a Document Processor preview fails, it shows the stored
representation with an explicit warning. Translation previews and exports fail
instead of quietly omitting the diagram. Font coverage and layout still need
visual review.

Raw model-authored SVG is not a supported extraction representation. Page
validation rejects inline SVG and SVG data images before canonicalisation can
discard their artwork. This is separate from the validated SVG generated from
accepted Mermaid diagrams during preview and export.

### Proposed reconstruction review

The next diagram contract should keep the source artwork and its page and
bounding-box provenance alongside a proposed reconstruction. Mermaid remains
appropriate for supported flowcharts. Bounded SVG primitives can preserve
geometric diagrams whose layout the Mermaid subset cannot express. Default
extraction should keep both representations for comparison, with the source
selected until review. An explicit vector-only instruction may omit the crop from
the extracted edition, but it must keep the PDF and the provenance. Selecting a
reconstruction must not delete the retained original.

Model-authored SVG needs its own validated semantic representation, not an
exception for arbitrary inline SVG. Geometry and stable label bindings should stay
separate from translatable text, as Mermaid already does. Admit only inert local
primitives with bounded dimensions and complexity. Reject executable content,
external resources, foreign HTML and unsupported styling. Review should compare
the original and the reconstruction, including label accuracy and relationships.
It should then store a revision-fenced choice that preview and all exports share.
SVG-only output still needs source comparison and accessible text. This proposal
is not implemented. Current extraction continues to use the accepted rule of
Mermaid or crop.

New extraction instructions require explicit `ol[start,type]` and `li[value]`
for visible ordinals, including page starts and returns from nested bullets.
Each nested list belongs inside its owning item. A split item's missing printed
marker is not permission to invent an ordinal. Complex compound legal labels
that HTML counters cannot represent remain literal paragraph text. Page
validation rejects malformed nesting, orphan items and invalid numeric
attributes. It does not prove that a valid number matches the scan.

The page merger keeps an incoming ordered-list start before it discards the
wrapper, counts direct items only and keeps reversed numbering. Conflicting
split-item ordinals or list styles fail explicitly. A continuation without an
explicit start keeps its historical incrementing behaviour. These checks protect
structural fidelity. They cannot detect a model confidently reading a printed 7
as 1, so source-page review is still necessary for that case.

Split list items merge explicitly continuing child lists recursively.
Independent child lists and repeated printed labels stay distinct. Join failures
identify the two physical pages and the section without exposing extracted text.
Packet preflight and worker validation run this same join within the batch and
against an explicitly supplied adjacent preceding-page boundary.

Continued tables keep physical page markers when repeated headers, captions or
column decorations are discarded. Relocated markers come before the incoming row
text, including a split row. Markers inside retained rows stay in place unless
their header is removed. A header-only fragment appends its boundary to the last
existing cell, or to an existing caption when there are no cells. With neither,
the marker stays before the empty table. Assembly does not invent rows or
captions. Page-scoped review and EPUB page-list links use these same markers.
This assembly repair does not rewrite completed documents or immutable Translator
source plans.

An assembly list conflict preserves the complete checkpoint and suspends new
passive runs until it is repaired. `carlina_document_recover` can also recover an
eligible historical failed run that has frozen source proof and every accepted
page batch. Inspect and bounded fetch expose the retained evidence. Preview
validates a topology repair that preserves text, attributes, list styles and
effective ordinals. Apply requires the snapshot revision, the exact preview token
and an idempotency key. In one transaction, it publishes a separate completed
execution, keeps the original execution and accepted responses, imports issue
sidecars and stores an encrypted repair audit. These cases block this bounded
recovery path: a changed source, a newer execution, incomplete evidence,
unresolved semantics, image-bearing pages, duplicate work units from product
retries and evaluator-finalised batches. It performs no new extraction or
provider calls.

New passive executions freeze `mcp_semantic_structure_policy=lists-diagrams-v1`.
Older passive executions omit the additional instructions, so retained request
fingerprints do not change. Managed extraction and prompt previews use the new
instructions. Existing documents are not rewritten automatically.

New passive executions also freeze
`mcp_image_accessibility_policy=image-descriptions-v1` separately. Managed
extraction and prompt previews use the current image guidance. Retained passive
executions without that marker keep their exact original extraction and
evaluation prompts and cache identity. Review continuations keep the frozen
marker, or its absence. The policy is part of prompt-cache identity and
execution fingerprints. Queued instructions and completed documents are not
rewritten.

The new instructions choose alternatives by purpose and surrounding context, keep
source captions separate from generated accessibility text, and require visible,
linked, structured descriptions for complex cropped diagrams and charts.
Editable flowcharts can carry a translatable short alternative and a context
description. Export also supplies a complete deterministic topology description.
Unreadable labels or uncertain relationships remain cases for the crop and the
issue ledger. The model must not guess missing data to make a description sound
complete. Evaluation reviews these source-grounded adaptations instead of
deleting them as unprinted prose. It still verifies the transcription against the
page, and it never marks generated descriptions as human reviewed. See the
[description contract](epub-export.md#structure-and-accessibility).

PDF image extraction and completed-document crop edits materialise the requested
bounding box with 0 px of added padding. Printed captions remain separate semantic
text, and the supplied box excludes neighbouring caption or frame pixels.
Retained original images allow later crop edits. A new crop or extraction applies
the current behaviour to an existing document.

Unicode text is preserved through page normalisation, assembly, UTF-8 semantic
storage and EPUB serialisation. Tests cover accented Latin, decomposed combining
marks, Greek, Cyrillic, pointed Hebrew, Arabic, CJK, historical forms and
ligatures. Grounding checks normalise text for comparison only, not for storage.
The existing footnote-marker contract deliberately converts numeric superscript
glyphs to ordinary digits inside the semantic superscript reference. It is not a
general ASCII conversion. New extraction instructions explicitly forbid
transliteration, accent stripping and spelling modernisation. These tests
establish deterministic preservation, not an OCR accuracy score for real scans.

Project namespacing shortens all of a diagram's IDs together. EPUB reserves a
stable ASCII family of IDs for each diagram and rewrites local links,
accessibility references and other supported ID references with the same map.
This prevents Unicode or long figure IDs from losing their label bindings during
export.

### Source-to-translation numbering synchronization

Current extraction review can correct numbering with revision-fenced scoped HTML
replacement and encrypted undo. Existing Translator snapshots and old exports stay
unchanged. Creating a new source plan from corrected bytes changes the
hash-derived block identities, so it is not a suitable numbering-only repair.

The explicit, one-way **numbering correction overlay** is stored encrypted for
each translation project. It is applied to the project's full source preview and
to the target render and export projection. Original snapshots, target text,
alignments and translation revisions stay immutable. The overlay may alter only
`ol[start,type,reversed]` and `li[value]`. Wording, item order, nesting, list
kind, IDs and diagram topology are outside this operation.

Preview compares three witnesses: the exact pinned source, its corrected Document
Processor revision, and the current translation and overlay revision. It binds the
owner, project, document, source-plan hash, selected element identities and old
and new attribute values. It admits only list subtrees that are uniquely matched
and unchanged apart from permitted attributes, with unchanged item text, topology
and protected references. It never uses fuzzy wording matches or positional
guesses after edits. Ambiguous matches, changed text or nesting, and stale
revisions require review. The comparison accounts for existing overlay values, so
a later source correction can explicitly replace a previous override or restore
the pinned value.

The owner UI and MCP expose preview, selected apply and undo. Apply holds the
source-plan writer fence, rejects active operations, rechecks the comparison and
stores an idempotent, revisioned receipt. Export requests pin that revision, and
workers reject stale deliveries. Regenerated exports receive the new projection.
Files already downloaded do not change. Target reconstruction and validation
finish before numbering attributes are projected, which preserves source offsets.
The compact block editor keeps the pinned representation, while the dedicated
full source preview shows effective numbering. Nothing propagates in the
background. A nonempty overlay blocks source-plan replacement. An empty overlay
permits a new plan and keeps its monotonic revision. History cannot restore
attributes from an older plan. History is bounded to 100 events and 128 receipts,
and reaching the bound gives an explicit full-history error rather than silently
losing retry or undo evidence. Apply reserves the count and byte capacity needed
to undo every outstanding change, so these bounds cannot strand a nonempty
overlay.

Clients keep global choices and concurrency limits, assign one execution owner to
each book, and bound model work for metadata, pages and translation. Different
books may progress in parallel, while list-heavy books use sequential page
batches. These client conventions add to the server's owner, lease and revision
fences.

## Remove marked running matter after extraction

The completed-document preview has **Remove headers / footers…** alongside its
editing controls. Select headers, footers or both, optionally restrict the
physical PDF page numbers, then preview. The confirmation button names the number
of elements to remove. The preview includes bounded examples and a count of
protected elements that will be kept. **Undo this cleanup** restores the
preceding revision while the panel remains open. Encrypted correction history
also supports later undo through the existing MCP review-history tools.

The shared service removes only elements explicitly classified with `page_header`
or `page_footer` classes. It does not guess from repeated text, position, generic
`<header>` tags, headings or note content. Images, page anchors, note references,
note structures and referenced targets are protected. A header that contains
protected structure is kept and reported. Page-scoped cleanup requires a known
physical page association for the entire element. Printed page labels may
disappear if they are part of a marked footer, but structural page anchors are
retained. Misclassified ordinary paragraphs need review corrections, not a more
aggressive heuristic.

MCP uses `carlina_document_cleanup` with `dry_run=true` by default:

```json
{"document_id":"…","roles":["header","footer"],"page_numbers":[1,2,3],"dry_run":true}
```

To save, send the same scope, the preview's `revision` as `expected_revision`, a
stable `idempotency_key`, and `dry_run=false`. Omit `page_numbers` for the whole
document. The editor and MCP share owner isolation, semantic validation,
optimistic revision fencing, atomic publication, encrypted inverse deltas and
idempotent retries. The existing retention bounds on correction history still
apply. If a very large inverse delta cannot fit, split the selection into page
ranges. A synthetic 649-page, 1,298-element cleanup fits and round-trips exactly.

Current HTML, Markdown and knowledge-source output are updated to match the
cleanup. Existing export files are not rewritten, and existing Translator source
snapshots are not changed. After you accept a cleanup, regenerate an export or
deliberately prepare a new translation snapshot. This feature does not clean any
existing user document automatically.

## Unicode identifiers and edit validation

Generated heading IDs keep Unicode alphanumeric characters, so the validator must
accept the same alphabet on later edits. Identifier values and local fragment
references follow a shared, bounded Unicode-alphanumeric rule. CSS class and
policy tokens remain ASCII-only. Existing identifiers are neither renamed nor
normalised. Document Processor, Translator rendering and the host export safety
guard agree on these references. Unsafe markup and invalid references still block
edits. The error now carries bounded diagnostic codes instead of source text.

The post167 regression was a mismatch between completion and edit. The first pass
generated accented IDs after attribute validation. The second pass removed and
regenerated those same IDs and raised `invalid_attribute_removed`. Stored hashes
and empty completion diagnostics therefore did not prove editability. Tests now
assert diagnostics as well as byte-level idempotence across multiple scripts, and
the 649-page cleanup fixture uses generated accented heading IDs. Real-document
acceptance uses read-only source acquisition and an isolated in-memory database,
so no production content is edited by it.

## Selectable worker transport

`carlina_work_claim` and `carlina_work_get` accept `response_mode`:

| Mode | Intended use |
| --- | --- |
| `standard` | Default compatible worker packet, including instructions, current evidence, output schema and native images. |
| `compact` | Omits system and developer messages, generation parameters and repeated submission guidance, with hashes and retrieval paths. Current user evidence, images and output schema remain. |
| `minimal` | Lease identity, workload and batch identity, and omission references, without task messages or images. Not sufficient to perform extraction alone. |
| `full` | Full diagnostic envelope and frozen request. `include_details=true` on claim remains the compatibility alias. |

`omit_fields` adds explicit omissions: `system_prompt`, `messages`, `images`,
`response_format`, `parameters`, `tools`, `submission_contract`. These are
transport projections only. Stored requests, output schemas, page identities,
replay fingerprints and validation rules do not change.

A fresh worker needs `standard`, even if its parent has already read the
instructions. Use `compact` only when the worker has read the omitted content and
its hash matches. An omission is never permission to ignore a constraint. Large
image bytes are removed before text transport. Native images are emitted only
when requested, without an unnecessary extra deep copy of the packet.

Use `carlina_work_get` with the current `work_item_id` and `lease_token` to
recover omitted content. It does not claim work, increment attempts or renew the
lease. It rejects a foreign owner, an ended execution and a stale or expired
lease. An optional `path` is a JSON pointer inside `/request`. For example:

```json
{"work_item_id":"…","lease_token":"…","path":"/request/messages/0","offset":0,"max_chars":12000}
```

Large text and JSON fields return an explicit slice, `next_offset`, lengths and
hashes. `sha256` identifies the original canonical JSON value, and `text_sha256`
verifies the paginated textual representation. Container reads replace image
blocks with references instead of filling slices with base64. Fetch an individual
image-block path to get its native image. Follow all required slices before
submitting. Never treat a truncated slice as the whole prompt.

## Scheduling and local work

### Delivery bytes and recoverable claims

Model input-token admission and tool-response transport are separate budgets.
`work_claim` and `work_get` use the saved response default of the authenticated
access key when `max_response_bytes` is omitted. Each key also has a maximum, and
explicit overrides above it are rejected before work is acquired. Existing keys
keep an 8 MiB default and a 64 MiB maximum. Both values are configurable within
64 KiB–64 MiB, with the default no larger than the maximum. `carlina_status`
reports the effective values in `work_delivery`. Use distinct keys for direct and
tunnel routes that have different limits. These are conservative transport
bounds, not a claim about any connector's undocumented capacity. Budget checks
include native images, both textual and structured projections, UTF-8 encoding
and JSON-RPC envelope headroom. Small packets remain inline. Oversized packets
return `delivery.mode=deferred` with lossless references. Exceptionally large
text also falls back to a small receipt with a paginated `/request` read. Frozen
instructions, output schema, model-context admission, page selection, image
resolution and bytes do not change.

For deferred images only, after reading the text and schema, call
`carlina_work_get` with the receipt's work ID and lease token, `image_offset=0`
and `image_limit=1` (up to four). The returned native image blocks keep their
request order and original JSON Pointer paths. The server returns the largest
contiguous prefix that fits the byte budget. Follow `delivery.next_offset` until
it is null. An image that cannot fit alone returns `work_image_too_large` with the
needed byte size or a lower bound. It is never silently resized or skipped.
Increase the budget only when the client can receive it and the key's configured
maximum permits it. Do not refetch images already inline when no evidence is
missing and `delivery.next_offset` is null. Existing individual image `path`
reads remain available. Inspect all required metadata evidence before submitting
the one complete metadata answer. Transport pagination is not partial metadata
completion or a smaller context window.

The operator can configure an existing key without rotating its bearer:

```bash
flask --app iarf_assistant.wsgi mcp configure-transport \
  --user-id 1 --key-id KEY_ID \
  --response-default-bytes 8388608 --response-max-bytes 8388608
```

Run this through the intended runtime and data-root configuration. This example
caps a tunnel key at 8 MiB. It is not an instruction to alter the managed
instance. `issue-key` accepts the same two options, and `list-keys` includes both
values. The setting controls passive-work delivery, not model input or output
token budgets or arbitrary exports. Runtime activation requires the additive host
migration. Source changes alone do not reconfigure an existing installation.

Supply a unique, stable `claim_request_id` for each intended claim. After an
uncertain response, retry with the same ID, authenticated key, execution, worker
and work-selection or lease arguments. You may change the response mode,
omissions or byte budget. The successful claim and its receipt are atomic. A retry
returns the same active lease, including any renewed expiry, without extending it
or incrementing attempts. Empty polls do not reserve the ID. Conflicting reuse
returns `claim_request_conflict`. An ended lease returns `claim_request_closed`,
never a different packet. Inspect the execution before using a new ID to acquire
new work. Receipts contain identity, fingerprint and attempt metadata, not
prompts or lease tokens. Legacy callers without an ID keep their existing
acquisition semantics.

Projection failures are caught after leasing and return redacted
`work_delivery_failed` guidance. Logs record correlation identity and error class,
and automatic deferral logs byte counts. They never record document content or
lease tokens. These logs show local preparation, not delivery acknowledgement by
a client. Retrying the same claim ID with `response_mode=minimal`, then fetching
bounded text and image batches, recovers from a lost or oversized response
without restarting the document or waiting for lease expiry.

Sequential extraction remains sequential. A completed multipage batch with a
valid saved boundary is no longer reprocessed on every resume, and batched
extraction does not rebuild irrelevant previous-page histories. Missing or
partial checkpoints still take the full processing and validation path.

New waiting checkpoints keep all outstanding work IDs in the current window. An
idle queue poll selects only their indexed identity and state columns, not
request or response bodies. It avoids reopening the PDF, encoding images or
replaying HTML until something changes. Any completion in the window wakes normal
processing. Cancellation or pause, a changed execution or queue binding, missing
work and invalid checkpoints bypass this fast path. The existing five-second
retry cadence remains. This optimisation removes work done during those retries,
not the durability checks themselves.

Concurrent batch plans accept an optional `max_in_flight` from 1 to 16. The
runtime default is 4. Only that bounded window is rendered and published while it
waits for external workers. Unscheduled ranges remain in the durable plan, not in
memory-only scheduler state. Legacy plan normalisation does not acquire new
fields unless they are explicitly supplied, which preserves existing request
fingerprints.

Use `wait_seconds` on work claims instead of a fixed 45-second sleep between
batches. `carlina_job_get` remains product truth: accepted transport output is
not proof of successful semantic validation. Its `include_details=true` view adds
the recorded local `packet_preparation` and `product_validation` calls, total
seconds and maximum seconds when available. Model inference is not included in
these local phase counters. Server checkpoints and paginated batch status make
runs resumable without a separate authoritative progress file.

The caller decides the batch page ranges explicitly. Do not increase dense German
batches merely because there is unused input context: output tokens, notes,
tables, images and semantic overhead can be limiting. A larger or balanced final
batch is appropriate only within the confirmed output budget. This release does
not silently replace existing plans or widen concurrency.

## Worker template and evidence

### Explicit blank pages and submission preflight

New passive PDF runs freeze `mcp_page_blank_policy=agent-declared-v1` and emit
the `document_page_batch_v2` schema. Every page has a required boolean `blank`.
The image-reading agent sets it true only when no substantive content is visible,
with empty `main_content` and `footnotes` roots. Carlina checks consistency. It
does not infer blankness from pixel density, and it does not trust noisy OCR over
that explicit visual judgement. Omitted printed TOCs keep their distinct omission
marker and `blank=false`. The accepted completion keeps the declaration and page
identity. Existing executions without the marker keep their original schema and
prompt.

Before accepting a submission, the Document Processor preflight hook checks the
new leased page packet's order, page roots, semantic markup, declaration and
explicit list continuity. A failure returns bounded field diagnostics, and the
same lease remains available for correction. A dry run performs the same checks
without recording acceptance. This pure preflight neither loads source files nor
validates source grounding. The worker remains responsible for full validation
and atomic publication.

Review diagnostics use the document's shallowest body heading as the root level.
Note reference locations and common heading context come from incoming
references, without inventing a source page for a collected note. Unreferenced
notes receive a neutral status. Broken targets and duplicate IDs remain errors.
The dedicated `carlina_document_repair_note_reference` operation restores one
source-verified missing marker, with revision checks, preview and encrypted undo.
Generic HTML edits continue to protect all existing note references.

### Local note sequences and relationship repair

The source resolver treats visible note labels and `fmX`/`fX` shorthand as local
evidence. A unique same-page match can resolve an unscoped marker. An explicit
`data-footnote-scope` permits a unique match within a witnessed chapter or
subsection sequence, including notes on later pages. A scope is an opaque token,
not a heading title or a guessed number. Repeated labels and identical note text
keep distinct note IDs. The nearest-page, last-used-label and unrelated
unused-note fallbacks are removed. Ambiguous markers stay visible and unlinked.
Bare superscripts remain candidates, since an exponent is not necessarily a
note. New default extraction guidance preserves true chapter endnotes as note
bodies and permits witnessed scope tokens. Work packets already issued stay
unchanged.

New runs also return bounded `note_arrangement` metadata: source `placement`
(`page_footnotes`, `chapter_endnotes`, `document_endnotes`, `mixed`, `none`, or
`unknown`), `numbering` scope (`document`, `chapter`, `section`, `page`, `mixed`,
or `unknown`), the witnessed `scope_heading_level` or null, up to 32 supplied
`source_pages`, and at most 800 characters of visual evidence. Placement and
numbering are independent: document-end notes may restart by chapter. A sampled
label of 1 cannot establish a restart, and a sample without notes cannot
establish their absence. The extension persists in canonical document settings
and travels in the bounded Translator source snapshot. Existing metadata columns
need no migration.

New full-note instructions use unique opaque marker and body IDs, explicit local
links only for known relationships, and separate printed labels. Repeated
labels, letters, symbols and punctuation survive matching without becoming
global IDs or an invented numbering sequence. Superscript digits keep the
established ordinary-digit presentation rule. The new passive marker
`mcp_note_arrangement_policy=source-arrangement-v1` freezes both the metadata
schema and the note instructions. Retained runs without it keep their previous
request bytes, including runs with the earlier native-vision policy marker.

EPUB export places linked local notes after their governing chapter or section
when the arrangement establishes that heading level and each note's incoming
links identify one unit. A long chapter's notes follow its final fragment. Notes
keep their source position when scope declarations conflict, when references come
from multiple units, when notes are unreferenced and when boundaries are
uncertain. Citation attributes take part in ownership, and inherited scope is
kept when a body moves. Wrappers containing the end of one chapter and the next
opener keep their text order around the inserted notes. Native list counters keep
their source values. Notes with printed markers use lists without a second
visible counter. This is an EPUB projection only: saved documents, translation
snapshots and other export formats keep their positions and identities.

`carlina_document_review_note_references` provides paginated existing references
and bare superscript candidates, with revision-bound `element_id`, zero-based
`marker_index`, label, href, page numbers and heading context. Keep the same
`expected_revision` while paging. Use the existing note inventory, source search,
fetch and physical page tools to establish the actual destination. Shared labels
or multiple references alone are not evidence of a wrong link.

`carlina_document_repair_note_topology` accepts 1–50 explicit operations:

- `promote_note`: `element_id`, `label`, `scope_id`; classifies one witnessed leaf
  list item, paragraph or aside as a semantic note, without moving its prose.
- `set_note_scope`: `element_id`, `scope_id`; annotates an existing semantic note.
- `link_reference`: `element_id`, `marker_index`, `note_element_id`,
  `expected_label`, `expected_href`; links or retargets that exact marker. Use JSON
  null for the href of a bare superscript. A body promoted in the same batch is
  a valid destination. Existing backlinks to a retargeted reference follow it.

Every operation includes its `action` name. Scope tokens contain 1–128 ASCII
letters, digits or `._:-`; visible text remains Unicode. The default dry run
returns the exact proposed edits. Save with the reviewed `expected_revision`, a
stable `idempotency_key` and `dry_run=false`. A batch uses the existing atomic
publication, encrypted undo and owner boundary. Generic edit guards are
unchanged. Stored semantic errors must be diagnosed separately: this repair lane
requires a valid current semantic document and rejects an invalid successor.

Relationship repair does not modify existing Translator source plans, translated
text or previous exports. Relationship repair for frozen translation plans is a
separate follow-up, and list-numbering overlays do not change note destinations.
Affected translated passages may also need targeted review if their earlier
context included the wrong note.

### Worker practice

Workers use native structured content, schema-derived page counts, semantic text
children, complete submissions and actual lease expiry. Send a normal response
once. A complete-payload dry run is optional. Avoid partial-payload probes.

## Metadata preparation performance

The metadata and structure path prepares its initial image window before issuing
the primary request. An expanded TOC request and heading requests depend on
earlier results. Page-batch windows also render their missing images before
dispatching concurrent external work. One backend queue worker can leave several
books waiting for external agents, so the per-book packet window is not a global
CPU or memory limit.

For new budgeted passive metadata runs, an unchanged wait reads only indexed work
identity and state columns before opening the parser. Pending or leased non-batch
work for the exact active execution suspends the job again. Completion,
cancellation, missing work, queue controls or changed ownership or state wake the
normal path. Page-batch checkpoints keep their existing wake-on-any-completion
behaviour. This prevents preparation on empty metadata polls. It does not cache
images across completed metadata stages. Rendering concurrency is a separate
mechanism, described below.

Both [PyMuPDF](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html)
and [PDFium](https://pypdfium2.readthedocs.io/en/stable/python_api.html#incompatibility-with-threading)
require process isolation for concurrent rendering. PDFium therefore offers no
threading simplification. MuPDF's standalone `mutool draw` also offers banded
rendering with native threads, but that path has not been benchmarked here.

Managed parsers share a process quota under the host-owned Document Processor
data directory. Each child opens its own document and calls MuPDF on its main
thread. Child-held OS file locks enforce the quota across book jobs and release
it on exit. Linux children arm an OS parent-death signal, and a parent-identity
check closes the startup race. Other platforms use control-pipe EOF, and a native
call holding the GIL can delay that fallback until it returns. Admission is
cancellable, and startup and page work have deadlines. A failed worker produces
an error instead of falling back to uncapped rendering. A conflicting worker
limit is rejected while any old slot remains active.

Pre-ready child exits get at most two additional startup attempts within the
original deadline and quota. Exhausted errors keep phase and exit-code
diagnostics. Stderr is drained with bounded memory, and only allowlisted
exception classes and content-free counts are exposed. Arbitrary child output is
never logged. New passive PDF runs suspend after recoverable renderer failures
and can resume through the same execution; see the
[MCP recovery contract](mcp-integration-architecture.md).

Metadata and heading evidence images are rendered and encoded in bounded chunks.
Workers pass file receipts instead of image blobs, and they exit before the
parser consumes the chunk or waits for a model. Unconsumed files are removed when
the consumer exits. Contact-sheet thumbnail preparation remains serial and has
its own timer. Page-batch rendering uses the same quota. External model packet
limits remain separate. Standalone parsers without a host data root keep serial
rendering. The existing image policy and encoded bytes are preserved. An abrupt
parent kill bypasses the parent's temporary-directory cleanup, so staged files
can remain under the output directory. Automatic orphan-file reclamation is
deferred. Ordinary exceptions, pause or stop, and early consumer exit clean
unconsumed files.

This limits active render processes and queued pages, not bytes or total RSS: a
single unusually large page can still use substantial memory. Access is
opportunistic between chunks, with no strict fairness guarantee. Alternative
renderers must preserve crop, rotation and annotation behaviour and text and
image fidelity, not merely improve elapsed time.

### Runtime timing counters

`preparation_timings` contains bounded numeric aggregates for metadata, heading
analysis, text-layer sampling, heading candidates, contact sheets, image
preparation and completion dispatch. Each stage records calls and elapsed time.
Method timers also record current-thread CPU time. Image chunks report worker
render and encode time sums, startup and quota wait, image bytes, and the sampled
peak aggregate RSS of that chunk's children. RSS sampling is an observation, not
a memory cap, and interrupted chunks can undercount unfinished work. Parent CPU
time excludes child CPU. Nested stage times overlap and must not be added to
derive job duration. Completion dispatch can include provider waiting.

The worker keeps sanitised preparation timings when metadata yields or fails,
using the active owner, document, job and execution fence. It updates only the
timing subtree, preserving current settings and controls. Document and job
projections expose these counters without prompts, images or extracted text. Job
queries require `include_details: true` for preparation counters, which keeps
routine polls compact. The small external-work summary is also present on compact
polls.

Exact execution projections also expose `external_work_timing`: pending, leased
and completed counts, the age of the oldest never-claimed pending item, and the
sum and count of the completed items' latest-lease-to-completion intervals. Those
intervals include agent, tool and transport time and are not inference
measurements. Earlier attempts cannot be reconstructed from the current lease
timestamp. The query reads only identity, state and timestamp columns. Above
5,000 items it reports `truncated: true` and null aggregates rather than
misleading totals. Historical execution queries keep their own external-work
measurements instead of inheriting a later run's preparation counters.

The current adaptive scale helper also references `page.doc`, which is absent in
the installed PyMuPDF version (`page.parent` exists). Its broad exception handler
silently skips image-resolution accounting. Correcting that behaviour, changing
encoding settings or replacing the renderer can change image bytes and therefore
passive request fingerprints. Preserve retained executions' image policy, and
introduce any changed rendering behaviour through an explicit new-run policy, with
output-quality acceptance.
