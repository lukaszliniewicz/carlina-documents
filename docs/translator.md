# Translator product contract

This document is the current product and runtime contract for Carlina's
Translator add-on. It records the behavior that must survive refactors and the
limitations that must remain visible. Ordered future work and exit gates live in
the [implementation plan](implementation-plan.md); current managed evidence
lives in the [project ledger](project-ledger.md).

## MCP adapter ownership

Translator registers all 39 product tools through the shared `McpToolRuntime`
port. Four modules own project/export, review/numbering/notes, glossary and context
tools, with unchanged discovery schemas and defaults. The gateway injects client,
serialization and source-page rendering callbacks. Translator's `mcp_execution`
module owns execution admission and deferred source-ingestion job association;
neither it nor registration modules can import the gateway. The gateway's startup
callback forwards to that coordinator for compatibility. Product validation,
source planning, persistence, queues and final publication retain their existing
implementations.

Creation normalizes language names, common aliases and ISO spellings before
persistence and idempotency comparison: `Hebrew`/`he` and `English`/`en` share an
identity. Source accepts `auto`, `auto-detect` and `detect`; the target must name
a language. Unknown inputs must match the bounded tag shape. This is syntactic
validation, not a closed language registry or a model-capability guarantee.

MCP creation settings are the closed, strict
[`TranslatorSettings` schema](../iarf_assistant/addons/translator/mcp_settings.py).
Supply creation options rather than copying a returned project's settings JSON.
Source references, captured provenance, settings/export versions and MCP execution
bindings are derived state. `annotation_profile` takes precedence over the
compatibility `enable_translator_notes` input; its persisted boolean is normalized
from that profile. Managed model identifiers and passive executor capabilities
have separate readiness rules.

Admission commits the MCP execution before initializing the client or starting
product work, releasing SQLite's writer slot. Initialization, startup and binding
failures mark that execution failed. A projection error after successful binding
does not change the queued execution. Creation still returns after frozen snapshot
capture and queue publication; its owner/queue-deduplicated ingestion execution
tracks source planning separately from translation.

## Operation admission and delivery

`operation_delivery` commits generic operation admission before lazily resolving
and publishing to the product queue. `block_admission` freezes and persists the
block workflow configuration in that admission transaction, including its source
pins, context mode and optional research dossier. The client retains forwarding
methods and supplies owner-scoped callbacks; the queue, operation ledger and MCP
execution ledger retain their separate responsibilities.

An enqueue exception records `queue_publish_failed` and fails the operation.
That code describes a failed publication call, not proof that no queue item
exists. Once an initial document has a durable operation, both synchronous and
deferred creation retain its frozen project, snapshot, plan and creation key.
A retry with the same explicit creation key recovers from those pins without
refetching the source. A late delivery of the failed attempt cannot claim it.
Initial planning failure before any operation still removes the incomplete
project.

A binding conflict rolls back and rereads the operation. An existing worker
binding supplies the authoritative queue identity; an absent binding leaves the
operation queued and unbound. A fresh queued operation keeps the single-flight
fence. If no worker binds it within the five-minute delivery grace, admission
reconciles it to `queue_delivery_lost`, allowing a fenced recovery operation.
Bound queued work retains its separate two-hour grace.

## Passive transport and revision-bound review

New aligned runs use writable-source v9: compact content and field values occur
once, with ordered sentence locators `{n, spans: [{field, start, end}]}`. Offsets
are half-open Unicode character offsets in each actual string value, rather
than its JSON escape encoding. Compact `content` is addressed before HTML
entity decoding; named field values are already plain text. Entity lexemes,
`<br/>`, protected markup and field order
are resolved against the immutable native plan. Completions retain v8's single
target copy, `<sb/>` boundaries and ordinal N:M groups. Saved v1–v8 packets keep
their original bytes and replay rules.

Retained-mode MCP claims advertise `mcp-retained-context-v1` fragment hashes.
`known_context` acknowledges exact reusable fragments; `known_target_revisions`
acknowledges precedent target revisions. Only acknowledged content is omitted;
there is no inferred worker/session cache. Clear acknowledgements after
compaction or transfer unless the content remains available. `work_get` full
mode or an exact request path recovers the frozen original. Trust delimiters and
current writable source are preserved. Sentence precedent remains advisory
source-wording similarity, with bounded token differences and lexical warnings
for changed negation, modals and numbers; these warnings do not prove semantic
equivalence or translation quality. Full mode remains exact and self-contained
mode does not use acknowledgements. Compact dispatch exposes measured input,
estimated output and frozen block/token limits without claiming which limit won.

For JSON tools, clients consume MCP `structuredContent` once, using the text
content only as a compatibility fallback. Passing both representations back
to a model duplicates the same evidence. The wire retains the protocol's text
representation for clients that require it.

MCP QA defaults to repeated-token groups and omits excerpts. Counts and capability
limitations remain scan-wide; group drill-down paginates every occurrence with
block/version/character locators. Individual findings and excerpts remain opt-in.
Integrity findings are never collapsed by a common token. Dictionary identity,
target revisions and query options bind cached diagnostics and cursors.

Review decisions retain their reviewed target identity. When the translation
changes, an earlier approval becomes stale and its effective decision is pending.
Review-fetch cursors include target and review-head versions;
`include_alignment=true` adds verified N:M groups when needed. Compact target
updates return the exact edit receipt's revision, version, editor hash and review
state; explicit full mode preserves the existing block readback.

Declared review coverage is append-only encrypted evidence, separate from
approval. Record structural checking, target-language reading or bilingual
comparison only after performing it, using exact target-version fences and a
stable idempotency key. Whole-block coverage or the union of reviewed source
anchors establishes completion for one kind. Agent/human provenance is explicit;
older prose is never retroactively marked reviewed. New target revisions make
their predecessors' coverage historical. Coverage reads default to 100 blocks
and counts without anchor-ID lists.

Exports default to draft (`review_requirement=none`). A required kind rejects
incomplete current coverage. Server admission freezes target identities and
coverage witnesses in the trusted operation checkpoint; its SHA-256 identifies
content, rather than authenticating arbitrary caller-provided snapshots. The
worker checks admission and targets before rendering, then fences target writes
while validating and committing publication. Additive coverage does not alter an
existing export snapshot. Exact retries retain the admitted manifest. Export
metadata exposes its requirement, hash and summary, without expanding prose.

## Snapshot-first creation and page-boundary repair

Current source provides asynchronous creation through MCP and the shipped browser.
The internal `TranslatorClient.create_from_document` and direct HTTP callers retain
the synchronous compatibility path unless `defer_source_planning=true` is supplied.
MCP always opts in; the browser derives the flag from the selected source type.

Creation captures and validates the source snapshot, then commits the project,
captured HTML, attestation/planning checkpoint and queued operation before
delivery. Content follows the [data-root storage policy](adr/0011-root-bound-ordinary-content-policy.md).
Queue payloads contain identity metadata, not source prose.
Sentence segmentation runs only in the visible `plan_document_blocks` worker.
Progress distinguishes source preparation, segmenter startup/connection and
segmentation, with processed-projection and discovered-sentence counts. An unknown
sentence total is not presented as a measured percentage. The workspace waits
for the plan instead of requesting nonexistent blocks. Premature starts return
`source_planning_not_ready`. Owner-only HTTP retry at
`POST /api/projects/<id>/source-planning/retry` reuses the captured source of a
failed setup; it does not fetch a newer extraction or restart a cancelled run.

MCP accepts an optional stable `idempotency_key`. When omitted, asynchronous
creation derives an owner-scoped key from the exact source content hash and
normalized creation request. An identical retry reuses the project and its
outbox delivery; a changed extraction or new explicit key permits a distinct
project. Ingestion execution receipts are also deduplicated by owner/add-on/queue
identity, including concurrent inserts. Separate projects can reuse the owner-scoped projection cache described below;
their immutable source plans and identities remain distinct.

Before creating a translation, Document Processor can inspect physical-page
continuations with `carlina_document_repair_boundaries` or **Repair page
continuations…** in the document preview. A scan is read-only and returns bounded,
revision-scoped candidates. Select up to 50 `{id, action}` decisions, where the
action is `join_with_space`, `dehyphenate`, or `keep_hyphen`; save requires the
preview's `expected_revision`, a stable `idempotency_key`, and `dry_run=false`.
The HTTP equivalent is `POST /api/documents/<id>/repair-boundaries`. Pagination
uses physical page numbers and at most 50 displayed candidates at a time.

No candidate is selected automatically: even a plausible split-word hyphen may
be intentional. Only adjacent, same-context paragraphs with matching attributes
are eligible. Headings, notes, lists, quotations, figures, verse, intervening
furniture and incompatible layouts are not crossed. Remove marked headers and
footers separately when they obstruct adjacency. Original physical page markers,
inline markup and reference IDs survive; one selected batch produces one revision
and one encrypted exact-undo history entry. Existing Translator snapshots and
plans remain immutable. Source repair affects subsequent snapshot capture only.

Passive Translator claims and renewals without an explicit lease duration use a
30-60 minute allowance, bounded by the existing one-hour maximum and based on
frozen output-token allowances. Other products retain five-minute defaults.
Explicit 30-3600 second requests override the policy; implicit renewal never
shortens a longer existing lease. Token, owner, version, expiry and cancellation
fences remain mandatory.

A work submission's `status=accepted` remains an acceptance receipt, not a target
commit. `receipt_state=schema_validated` and `product_commit_confirmed=false`
retain that boundary. Current v8 passive Translator packets additionally receive
pure, packet-local preflight before acceptance: result cardinality, inline and
protected-literal preservation, marker shape, complete nonempty units, field
names and monotonic N:M alignment coverage. Failures identify a local block
number, JSON path and bounded code, leaving the lease available for correction.
`dry_run=true` performs the same checks without recording a submission. A native
add-on hook supplies this validation; core does not import product normalizers.
Preflight does not reload the whole source plan, call a provider or commit targets.
Full source-bound product validation and activation remain with the worker.
Historical packet transports retain their original worker path.

New durable retries carry a versioned identity in their authenticated preparation
event. Passive dispatch includes that identity and bounded validator codes, so a
post-accept normalization failure receives fresh claimable work instead of
replaying the rejected completion. Default and reduced MCP claim views retain
`request.dispatch.translator_window_retry`, including attempt identity and safe
validator codes, so the worker can correct its next response. Re-entry of the
same retry keeps its work ID.
Previously prepared retries without that marker retain their original dispatch
and leases; historical events and frozen provider packets are not rewritten.

The translating executor owns linguistic sentence boundaries. The punctuation
heuristic no longer vetoes a model-authored unit because it contains a domain
name, abbreviation, note label, quoted aside or several grammatical sentences.
It also does not silently resplit the target. Exact projection coverage,
nonempty ordered ranges, complete source/target alignment, protected resources,
markup validity and stored integrity receipts remain mandatory. This is a
structural validator, not a claim to certify grammar or translation quality.
The versioned deterministic splitter and already-saved partitions are unchanged.

MCP job polling exposes bounded `block_failure` metadata and separate
`translation_progress` counters: total, translated and remaining blocks, fixed
source-anchor count, and an actual translation percentage. Counters use persisted
active targets with valid revisions, not schema-accepted submissions, staged
candidates or the operation's 50-percent setup milestone. They do not decrypt
book text and explicitly are not a fresh integrity audit or export-readiness test.
Fine-grained attribution for every final worker validator remains future work.

`carlina_job_get` selects the operation by the requested execution's owned project
and queue, preserving terminal execution truth and withholding current-project
output/progress after reuse. It never falls back to another run's operation.
`waiting_external` requires pending or leased work on that owner/project/
queue-bound passive execution. Once the response is accepted, polling reports
`running` while Carlina stages and finalizes it, even before the queue consumes
the handoff. A terminal operation reports its terminal outcome; finalization
failure preserves accepted evidence for recovery instead of asking another model
worker to wait on a nonexistent lease.

`carlina_translator_configure_batches` changes the input-token budget and block
cap on the same project using the returned settings-version fence. It requires
no active operation or pending/leased passive work, preserves the existing source
plan and translations, and affects a subsequently resumed operation only. Do not
create another translation project merely to tune batch size.

## Reusable sentence segmentation

New asynchronous aligned-source planning uses an add-on-owned SQLite offset cache,
separate from projects, targets and the main database schema. Keys bind the owner,
exact projection, source language, planner settings, strict offset adapter and
splitter identity. Target language, prompts, annotation choices and translation
windows do not affect reuse. Unchanged projections can also be reused across
corrected snapshots; project-specific plans and identities are never copied.

The first-party splitter identity hashes the loaded ONNX artifact, tokenizer,
model configuration, service code, package versions, active execution providers,
inference batch size and CPU threads. The adapter freezes stride/block size once
and verifies identity on every response. An identity change during interrupted-plan
retry is rejected. Providers without this identity API remain uncached; completed
historical plans are never resegmented or enrolled retroactively.

Entries contain keyed opaque witnesses and authenticated sentence offsets, not
source or target text. Exact coverage and receipts are validated on cache hits;
corruption causes recomputation. Writes are batched every 16 distinct projections.
Committed batches survive interruption; up to 15 unflushed entries may be recomputed.
Retention is capped at 100,000 entries at planning admission.

An expiring data-root-wide SQLite lease serializes new aligned-source plans before
segmenter startup. Waiters report resource waits and renew their product lease.
Inference holds no SQLite write transaction. The cache lease expires in 15 minutes,
is refreshed during processing and fences stale writers. The service separately
allows one active inference and at most four waiting calls, with four CPU threads
and batches of four model windows by default. Health reports resource counts;
planning reports cache hits/misses. DOM and final source-plan persistence are not
yet fully streaming. No whole-book RAM or throughput improvement is claimed.

## Product boundary

Translator turns one immutable semantic document into a source-bound target
document. Translation is an editorial workflow, not a fire-and-forget text
completion:

- the normal workspace presents source and target side by side;
- source and target use the same editorial body typography and render inline
  structure as formatting rather than transport markers;
- headings, lists, tables, captions, notes, links, inline structure, protected
  literals, and block order remain part of the translation contract;
- completed or human-edited targets are never silently regenerated by
  `Translate remaining`; an owner may explicitly select one block, a
  Ctrl/Cmd-click set, or a Shift-click range for retranslation;
- glossary usage guidance and reader-facing translator annotations are distinct,
  durable editorial objects rather than prose hidden only in a model prompt;
- attention states come from actionable validation or conflict evidence, not
  merely from unapproved model-reported or count-only alignment evidence.

The document-block runtime also provides these review boundaries:

- a project may accept externally prepared analysis and research conclusions,
  provided their source-plan scope and unverified provenance remain explicit;
- accountless capability links may grant bounded viewing or human review without
  granting project administration or model-spend authority; and
- humans and governed model clients may inspect, search, and revise a partial or
  complete translation through bounded document-native contracts, while hashes,
  candidate signatures, token IDs, locators, and internal alignment receipts
  stay out of the ordinary review surface.

The canonical block editor presents each semantic target block as one continuous
editing surface. In aligned-sentence mode, clicking a target sentence opens a
focused editor that preserves its segment boundary even when punctuation changes.
Explicit **Split at cursor** and **Merge with next** actions change that partition;
merging across groups combines their source membership. The complete block editor
remains available for broader changes. A safe source presentation retains visible inline
formatting; the target renders protected formatting semantically and exact
literals as quiet immutable inline content. Candidate text-part boundaries,
token/literal markers, and button-like chips are transport detail, not document
sections. Saving flattens the editor projection back to the strict candidate
contract without changing source-owned identities.

The current source implements this through `translator-block-workspace-v5` for
block mode, `translator-block-workspace-v6` for aligned-sentence review, and
`translation-block-editor-v1` for canonical edits. Source and target are
projected into the same restricted inline-markup shape; the browser renders that
markup as formatting, and the block editor remains one continuous editing surface.
In aligned-sentence mode, verified model-reported groups are shown as
quiet child rows of their semantic block and support keyboard movement across
the loaded document. Each row pairs its source and target sentence ranges on
one shared grid, including one-to-many and many-to-one groups; sentence text
uses its own writing direction. Only phone-sized layouts stack the two sides.
Block cards keep notes, review decisions, and the formatted-block toggle in a
compact icon header. The notes icon shows the current note count and marks
annotations needing review; opening it reveals the existing editor. Inline
note links can still expand that panel. Review choices open from the status
icon, with the saved decision reflected in its label and appearance. Pairing
provenance remains available from the header information icon. Source and
translation labels appear once per card; arrow-key navigation remains available
without a repeated instruction row. Note/review saves apply the API's returned
entity projection directly, preserving its version without reloading the page.

Workspace reads continue to decrypt and validate the persisted source rows,
project ownership, source pins, counts, and hashes. The final pure source
identity reconstruction reuses successful validations within the current
process, keyed by a digest of the complete plan DTO. This bounded cache stores
only 128 digests and never caches failures or document content. Unchanged plans
avoid repeated parsing and sentence segmentation; a new or changed plan, an
evicted entry, or the first read after a process restart still performs the full
reconstruction. Target and alignment validation remain unchanged.

The server reparses every save through the same allowlist
and restores the full source-bound candidate before validation. A manual edit
creates a new target revision. Focused sentence edits preserve the chosen
partition under `editor-segment-v1`, with reader-edit provenance requiring review;
block edits preserve valid previous boundaries or use a labelled automatic
fallback. Neither claims the earlier model report still describes changed prose.
Sentence saves use the version at editor opening and stable retry identity;
conflicts keep the draft visible. Immutable literals, formatting, attribute
suffixes and untouched text survive replace, split and merge operations.
Human editors may add only target-local emphasis
(`em`/`strong`, including their `i`/`b` equivalents), underline, superscript,
or subscript through selection controls; bold, italic, and underline also use
their standard keyboard shortcuts. These formats are stored as narrow target
parts and ignored by text alignment. Strikethrough, highlight, arbitrary
elements, attributes, layout, and active markup remain forbidden because they
carry editorial or structural meaning beyond ordinary inline typography.
Provider output stays on the smaller source-owned formatting contract, so manual
styling does not add prompt or schema work for the translation model. Workspace
v5 sends only the authenticated access role, active source-plan ID/version,
block identity/order/role, safe source and target editor projections, target
compare-and-set version, current reader annotations/review state, and user-facing
validation state. Workspace v6 adds only the review mode and verified alignment
groups expressed as ordered source/target sentence text; internal anchor/span
identities, confidence, methods, evidence, and receipts remain server-side.
Raw fragments, planner slots/tokens/wire, candidate lineage, rendered target
evidence, and alignment receipts stay on the server. Complete-document preview
and export remain separately gated on a complete, valid target projection.

The toolbar's **Glossary** button opens a large, centered, scrollable modal;
no persistent glossary rail consumes bilingual reading width. Entries load on
first opening, and closing/reopening preserves an unfinished term draft and
restores focus to the launcher. Reader annotations remain attached to their
blocks. Image descriptions use the
same plain editorial source/target treatment as other text instead of repeating
`Image description`, `Accessible text`, and `Alt` chrome around one value.
Boundary-only fixed whitespace remains canonical in the editor DOM and save
payload but is hidden from the visual baseline.

Document Processor owns semantic ingestion. Translator consumes its immutable
processed-document snapshot and does not maintain a parallel lossy extractor.
Only semantic source blocks are writable: page/running headers and footers are
context or marginalia, never target prose.

New aligned-sentence projects prepare their source inventory with the configured,
owner-scoped Sentence Segmenter before the immutable plan is created. Each exact
block projection is segmented once, with duplicate projections sharing the
result. The returned strings must preserve all non-whitespace source characters
in order; whitespace remains in the contiguous, full-coverage anchor spans.
Unavailable, malformed, rewritten, or incomplete results fail project creation
without publishing a partial project. Block mode does not require the segmenter.

The plan stores a versioned `source_sentence_manifest` of projection hashes and
end offsets, not duplicate source prose. Its bounded contents participate in the
plan's settings fingerprint and identity. Canonical validation, persistence
rehydration, and source-correction replay use those saved boundaries without
calling the segmenter again. Existing plans retain their original anchors;
changing an unprepared block-mode project to sentence mode is rejected with an
instruction to create a new project. Existing sentence-mode projects remain
readable without silently rewriting translations, alignments, or notes.
Alignment-only regeneration for those older projects is deferred.

## Source-structure corrections

Workspace v5 exposes **Merge with previous**, **Merge with next**, and
**Exclude from translation** through one quiet block-header menu. Each action is
an owner-authored, append-only successor source plan over the same attested
document snapshot. The predecessor plan and correction receipt remain durable;
no action edits source bytes or deletes source evidence.

Merge is deliberately strict. Adjacent blocks must have the same semantic role
and either a whitespace-only gap or the same exact enclosing semantic wrapper.
The successor presents one natural translation unit while source identity still
reconstructs byte for byte. A complete inline wrapper shared by both inputs is
omitted from the model-facing fragment and reapplied once by the host, while the
editor still renders its typography. A pair such as two fully italicized
paragraphs therefore does not spend model attention on ceremonial close/open
tags. Exclusion reclassifies the selected coverage as explicit owner
marginalia and omits its exact standalone wrapper from target rendering; it is
not deletion.

The operation is unavailable while translation/export work is active or the
browser has unsaved target drafts. An explicit active-plan ID/version provides
the compare-and-set fence. Only blocks with identical semantic source identity
may carry a verified active target into the successor, and each carried target
is repersisted under the new plan with a reuse receipt. Merged or excluded
blocks become pending. Existing plan-scoped analysis, research, generated
glossary proposals, alignment, and memory evidence remain historical and are
never silently rebound; current owner-locked project glossary terms remain
available when the successor workspace reloads.

Translator is trusted native code owned by the Documents repository. Private
Carlina consumes the same source through a pinned composition. Source ownership
is separate from the later installed-generation ownership transfer.

## Durable workflow

One document-block operation follows this order:

1. Verify the processed-document snapshot and persist an immutable ordered
   source plan with source hashes, protected slots, and structural anchors.
2. Freeze model identities and capability profiles, workflow settings, research
   tool declarations, source lineage, every already-active target pair, and
   either the pending-block scope or one exact owner-selected block set. The
   next operation may override the project's translation model and reasoning
   preference without mutating project defaults or an operation already in
   flight.
3. Derive the analyzer's exact ordered scope plan. An operation may adopt a
   verified historical analysis trio only when the source, execution identity,
   phase contract, budget, and scope plan match exactly. Otherwise analyze the
   document: use one whole-document packet when the frozen counter admits it,
   or heading-aware complete-block packets that cover the whole source without
   prefix truncation.
4. If preliminary research is enabled, reuse one exact completed historical
   run when eligible or create one durable document research run after analysis.
   Fresh research must run even when analysis reports no explicit unresolved
   items; in that case the task seeks terminology, entities, genre conventions,
   cultural or historical context, references, and translation traps. It must
   use at least one selected read-only tool and report irrelevant evidence
   honestly rather than manufacturing support.
5. Freeze the operation baseline from analysis, research evidence when present,
   and currently active human-locked glossary revisions.
6. Plan adjacent translation windows with the frozen model counter and the
   project's independent batch-input target. `Translate remaining` treats every
   already-active target as a protected boundary. Explicit retranslation omits
   every unselected block instead, so a selected active target may advance by
   compare-and-set while its previous revision stays visible and durable until
   the replacement succeeds.
7. Assemble each window from typed context slots: trusted control instructions;
   untrusted source, structure, research, metadata, memory, and exact-repeat
   precedent evidence; host-verified source lineage; and frozen human glossary
   constraints. Exact-repeat precedent does not require an embedding model: a
   current human-edited wording is preferred, then the nearest preceding active
   target. It is strong advice rather than substitution, so local context may
   justify a variant and no prior occurrence is silently rewritten. Profile v5
   continuity likewise sends only the predecessor's natural source text, role,
   and compact target markup/fields; durable candidate IDs, hashes, token IDs,
   and lineage remain host-side. Writable
   source v4 exposes each complete block as natural inline markup plus named
   lexical fields. Complete outer formatting/link wrappers are implicit because
   their placement is deterministic; the host restores them around the accepted
   target. Only packet-local block numbers and references for internal structure
   cross this writable boundary; the canonical planner DTO, durable IDs, hashes,
   offsets, source-owned attributes, and immutable source values remain private
   validation evidence.
8. Require one compact result for every local writable-block number. Native-
   schema providers receive a closed JSON schema; strict-text providers receive
   that same closed result contract and exact local result count. The host binds
   each local number to the frozen window, restores source-owned structure, and
   normalizes the result to the durable target candidate before validation.
9. Normalize, validate, and stage candidates before compare-and-set activation.
   Preview and export remain unavailable until the source plan has a complete,
   structurally valid active target projection. A complete source document
   retains its existing document shell; an attested semantic fragment receives
   exactly one target-only language/body envelope after reconstruction. The
   immutable source snapshot, hashes, and block offsets are never rewritten to
   manufacture that export shell.

Provider output is evidence, never authority. Every candidate is checked
against immutable source lineage and protected structure before it can become
active.

Image alternatives remain lexical attribute fields. Marked short alternatives
for editable diagrams and structured image descriptions use the same ordinary
paragraph/list/table blocks as other prose. Their marker classes, IDs, links,
caption structure and literal graph template stay source-owned. Only their text
and graph labels translate. Target validation checks the closed description
structure and local references before accepting the candidate. The common export
then derives complete node/connection tables from translated labels, preserving
inner language changes and leaving the source snapshot immutable. This uses the
existing provider contract rather than a separate description response channel;
the [EPUB contract](epub-export.md#structure-and-accessibility) governs projection
and human review.

Strict-text output must be either the exact JSON object or one otherwise-empty
JSON Markdown fence. The host first performs ordinary strict JSON parsing. If
that alone fails, it may escape only a bounded unescaped quote that cannot
legally close its current JSON string—for example, the ASCII closing half of a
typographic quotation in translated prose—and parse once more. This changes
serialization only: decoding restores the model's exact character. Missing
delimiters, prose wrappers, invented fields, cardinality drift, malformed
compact markup, and source-owned structure are never repaired. The complete
closed response contract and deterministic candidate validators still run
before anything can be staged.

The default writable contract is `translation-block-writable-source-v4`;
new aligned-sentence operations select v8. Their `content` is the same deliberately small
XML-like fragment:

- ordinary prose and pure formatting use natural tags such as `<em>`, `<b>`,
  `<sup>`, or `<br/>` without IDs or classes;
- a chain of formatting or link elements that wraps the complete block is
  omitted from the provider packet and restored exactly by the host, because it
  has no target-language placement choice;
- an element receives a short local `r` reference only when the host must restore
  source-owned attributes or a link/reference target and that element remains
  inside the writable fragment;
- exact visible source text that must not be translated uses
  `<lit r="N">…</lit>`;
- translatable attributes such as approved alternate text or labels are named
  entries in `fields`; and
- block mode does not ask the model to report sentence splits or merges. It
  translates one semantic block naturally, while target sentence spans and a
  count-based alignment are derived after acceptance;
- optional aligned-sentence mode freezes `translation-block-writable-source-v8`
  for new operations. Source sentence anchors remain read-only. The provider
  writes target prose once in the formatted `content`, adds empty `<sb/>`
  sentence-END markers (including the final unit), and returns `alignment` with
  exactly `groups`. Each group has ordered `source` and `target` ordinal arrays.
  The host derives target sentences and exact spans from its normal canonical
  text projection, then validates complete noncrossing coverage. The model
  cannot write durable IDs, character offsets, hashes, confidence, or review state.

A v8 result can contain `First.<sb/> Second.<sb/>` with
`"alignment": {"groups": [{"source": [1], "target": [1, 2]}]}`. There is no
`target_sentences` response field. Markers may sit inside existing emphasis/link
wrappers spanning sentences; wrappers and protected references are never duplicated.
Place closing punctuation, quotations, and attached footnote references before
markers. Empty units, missing final markers, nonempty/attributed/namespaced
markers, markers inside literals or void elements, and active XML are rejected.
Escaped literal text spelling `<sb/>` is not a marker. Existing canonical entity
decoding, immutable literal expansion, and whitespace/span rules remain in force.

For a field-only block (for example image alternate text), its single translatable
`fields[].value` is an XML-escaped text fragment containing text and `<sb/>` only;
`content` stays empty. The field is decoded and markers removed before creating
the ordinary lexical attribute candidate. Markers never reach target revisions,
reader content, or exports; the encrypted provider transcript retains the original
wire response for verification and recovery. The internal sentence-manifest and
span contracts are unchanged. New output budgets charge prose and marker overhead
once, not a second complete target copy.

Frozen v7 operations keep their original duplicated `content`/`target_sentences`
response contract, prompts, schema, and budget on dispatch and recovery. Existing
source plans, anchors, translated revisions, and their hashes are not migrated.
Starting another operation on an existing project selects v8 without replanning.

Aligned sentence validation permits clear English and Polish continuations after a
quoted question or statement, including an explanatory dash or lowercase speech
attribution. This validation is separate from the versioned deterministic
splitter: historical target-span boundaries and their receipts retain their
original behavior.

No source class, durable ID, hash, locator, token ID, or attribute value is
model-writable under either mode. Omitting a complete outer wrapper does not
discard its tag, link target, attributes, or provenance: those remain
source-owned host evidence and are restored before validation. Internal or partial formatting,
void elements, literals, and lexical fields remain explicit and exact. The
compact fragment is not persisted as target truth:
it is restored to `target-block-result-v1`, deterministically validated, and
only then staged. This retains exact provenance without charging the language
model an attention tax for deterministic wrapper ceremony. Writable v1, v2,
and v3 packets stay replayable for attempts that froze them; response
normalization is bound to the exact frozen writable version, including crash
recovery. v3 still requires every wrapper it originally exposed.

Model capacity is a hard safety ceiling, not the desired translation batch
size. Each project stores its own translation-batch controls. New projects
default to a 16,000-token total-input target and at most four semantic blocks per
translation call. Owners may configure 2,048–1,000,000 tokens and 1–256 blocks
for that project's next operation. The effective token target is
clamped to the selected model's frozen usable input capacity. Planning measures
the fully rendered prompt—including shared analysis, research, glossary,
metadata, and structure evidence—and chooses the largest adjacent set of
complete source blocks satisfying both limits. It never slices a block. The
token target remains the primary size control; the block ceiling prevents a
context-heavy operation from suddenly packing dozens of individually small
headings, notes, or labels into one unwieldy response. Selected
operation-baseline and per-window structure evidence remain required. New
`translation-context-v2` runs reduce optional continuity, segment precedents and
memory before shrinking writable source. Historical v1 packets retain their
original non-discardable continuity, exact-repeat and memory behavior.
Output-shape estimation independently prevents a window
whose source-bound JSON result is unlikely to fit the model's frozen output
ceiling.
The lower bound is an operational attention/latency control, not a model-capacity
claim. Small targets may produce singleton windows; an indivisible block whose
fully rendered required packet exceeds the chosen target is rejected explicitly
so the owner can raise the target instead of silently slicing source structure.
The effective input target, actual measurement, counter method, and model
capacity remain frozen with the operation. The reserve is admission evidence,
not a generation instruction: current translation calls omit the provider
output limit and let the model stop naturally. Historical window profiles v1-v3
retain their original bounded provider projection; v4 retains its unbounded
provider projection, while current v5 adds exact-repeat precedent. Writable-
source v1/v2/v3 attempts likewise retain their original projection for exact
reconstruction. Analysis and research keep their independent bounded output
policies.

Reasoning effort is an explicit per-phase preference, not a hidden project
preset. Translator exposes separate controls for translation, terminology
analysis, and research. A preference is bound to the selected model, persists
for the owner across Translator projects, and is frozen into the next operation;
changing it cannot mutate work already in flight. The UI offers only levels
recorded in that model's capability metadata. If a model claims reasoning
support without declaring configurable levels, Translator uses the provider
default instead of inventing choices. An explicit `off` is sent through the
shared model adapter as the provider's no-reasoning setting when the model
advertises it.

The workspace also offers a compact next-run model/reasoning control. Its
override belongs only to the newly created operation: stopping an operation
does not rewrite its frozen config, and resuming means starting a fresh scoped
operation after cancellation becomes terminal. The owner can then retain the
saved reasoning preference, request the model/provider default, or choose one
advertised level for the selected model.

## Passive MCP execution

Managed and passive execution share the same `TranslationOperation`, frozen
source plan and configuration, cancellation, partial/no-replay behavior,
active-pair compare-and-set fences, preview, export and evidence graph. Passive
execution freezes a declared, task-authorized external executor profile. Its client claims a
versioned completion packet under an owner-scoped lease and submits the exact
closed result shape; it does not write target rows, durable IDs, source attributes
or active pointers directly.

Supported packets receive contract preflight before submission acceptance. The
product worker then performs source-bound normalization, structure restoration,
validation, staging and activation. An accepted submission remains a receipt,
not a committed translation. Lease expiry, cancellation and revision fences
protect activation; exact retries preserve accepted work and human edits. See
[failure and continuation](#failure-retry-and-continuation) and the
[MCP execution contract](mcp-integration-architecture.md#execution-model).

Harness-side browsing or tools are outside Carlina's execution-policy receipts.
Externally authored research conclusions remain external and unverified. If the
harness separately invoked a governed Carlina tool, its receipt may prove only
the cited source retrieval and retained content hash—not the harness's synthesis.
Neither can be silently promoted to the operation's ordinary verified research
graph. The shared gateway, protocol compatibility, source ingress, human-review
handoff, authentication, and external-work ledger are specified in
[MCP integration architecture](mcp-integration-architecture.md).

## Analysis and research

Preliminary analysis and preliminary research are distinct phases. Analysis
derives a document brief, style guidance, entity observations, terminology
proposals, and unresolved questions. Research may use only the exact frozen
read-only tools selected by the owner; live provider, tool, connection,
function-binding, and schema identity are revalidated before dispatch. A worker
loads the exact immutable add-on roots and compatible dependency closure needed
by those frozen declarations; it does not assume that Translator's own static
runtime closure contains Web or Library providers.

The ordinary research picker has three presentation groups:

- **Web** recommends provider-declared general search and page-reading tools;
  Jina is the default preference for ordinary text search and scraping when the
  declared surface offers it;
- **Library** is reserved for owner-scoped Knowledge collections; its executable
  host picker and collection manager are not implemented yet; and
- **Advanced** contains connector-specific or specialist tools without making
  provider plumbing the default user experience.

Current block-research limits are deliberately generous: at most 128 model
calls, 64 tool calls, and 128,000 requested output tokens per model call,
clamped to the selected model's verified native maximum. Each retained result
may occupy up to 2 MiB. These are ceilings, not a target consumption level.

Schema-invalid tool arguments are rejected before claim or I/O and retained as
witnessed correction feedback for the model. Other valid calls from the same
assistant turn still run. A completed research conclusion may retain that
pre-dispatch correction history, but only successful tool calls count as
external evidence; claimed failures, uncertainty, policy blocks, cancellation,
or an uncorrected final tool request keep the run non-admissible.

New operations freeze `document-block-research-input-planner-v3`. The planner
renders the real provider request, including the selected read-only tool
schemas and frozen reasoning configuration, and admits it against the research
model's frozen native-context profile and runtime token counter. A research
source message has a 4 MiB hard transport limit inside the existing 8 MiB
provider-packet envelope; ordinary analysis, provider responses, tool results,
and events keep their smaller bounds. Model-facing blocks use the same compact,
language-first inline markup as translation windows plus their durable citation
ID; internal locators, hashes, anchor IDs, slot IDs, and protected-token IDs do
not consume model attention. The planner supplies every ordered semantic source
block when the full document fits both the model and transport envelopes.
Otherwise it supplies the largest
source-ordered prefix of complete blocks that fits and records the exact
included IDs and omitted count; it never uses a fixed four-block limit or
slices a block. The semantic brief, document metadata, source scope, and exact
request plus its admission evidence remain encrypted durable evidence. A run
uses tools selectively and stops once material uncertainties have sufficient
evidence; the generous limits are safety ceilings, not a research quota. A
frozen operation is never silently upgraded to a different source projection;
an operation using an unsupported earlier planner must be restarted.

Cross-operation reuse is direct reference, not evidence copying. The new
operation owns an authenticated selection receipt that pins the origin
operation and workflow, three immutable analysis artifact revisions, and an
optional completed research graph. Analysis eligibility requires exact owner,
project, source plan and content, languages, engine and analysis contracts,
model revision/profile/reasoning, non-secret endpoint transport, output budget,
and ordered token-aware scope plan. A pure secret rotation at the same endpoint
does not invalidate already-produced evidence; an endpoint, API-version,
model/profile, planner, or scope change does. Snapshots created before the
transport or scope-planner witnesses existed are not eligible.

Research is selected independently and adds exact research model, reasoning,
budget, provider, add-on, connection, and tool declarations plus a fully
completed, receipt-verified conclusion graph. Current source-seeing evidence
must also verify the initial v2 packet, exact source scope and projection,
effective output request, and packet hash. Thus exact analysis may be reused
while changed or disabled research is omitted or run afresh. Partial,
nonterminal, legacy brief-only, ambiguous, cross-owner, drifted, or tampered
evidence fails closed. Only the signed operation receipt may authorize a
baseline to reference a research run owned by another operation.

Current limitations are explicit:

- when the whole document does not fit, preliminary research currently sees
  one maximum admitted complete-block prefix rather than a map/reduce scan of
  every source block;
- there is one document-level research run per operation; translation windows
  cannot initiate research for newly discovered local ambiguities;
- passive translation responses cannot mutate glossary policy directly; live
  terminology edits use the separate model-glossary API, while optional research
  dossiers supply a deliberately pinned preparatory baseline; and
- Library collections cannot yet be selected, created, or managed from
  Translator.

The implementation plan orders proposal review, window-scoped research, and
Knowledge collection binding without mutating a frozen baseline or an
already-prepared window packet.

### Externally prepared scholarly context

Status: **implemented for externally submitted, explicitly unverified
conclusions.**

Carlina-managed analysis and tool-using research are one way to create evidence,
not a mandatory monopoly on thinking. An owner or authorized orchestration client
must be able to submit one immutable **research dossier revision** after doing
the work elsewhere. This supports a coordinator that asks several specialist
agents to investigate terminology, bibliography, allusions, historical context,
and translation traps, then records the synthesized conclusions without asking
Carlina to repeat the same web research.

Each dossier revision is bound to the owner, project, exact source plan and
language pair, plus a whole-document or exact block scope. Its closed payload may
contain a document brief, style guidance, entities, terminology proposals,
references and allusions, unresolved questions, proposed reader annotations,
and bounded citations or evidence claims. It records submission time, a client
submission ID and digest, the declared external method/models when supplied, and
whether Carlina has independent tool receipts for any cited evidence. Raw agent
transcripts are optional and are not required merely to make the conclusions
durable.

An externally submitted dossier's conclusion provenance remains
`external_unverified`. A governed Carlina receipt may separately prove that a
cited source was fetched and bind the retained source bytes/hash, but it does not
certify the external agent's synthesis or recommendations. Only a fully Carlina-
governed dossier-generation or validation run whose receipt binds the exact
dossier content hash may assign stronger managed conclusion provenance. This
distinction limits the claim, not the dossier's usefulness: external conclusions
remain reviewable, reusable, and eligible to be deliberately frozen into a later
operation. Terminology and note proposals remain proposals until an authorized
human promotes them. Replacing a dossier appends a revision; it never changes the
baseline or packets of an operation already in flight.

Every externally supplied field is untrusted evidence, not executable policy.
Submission cannot enable a tool, choose a provider, broaden authority, or inject
control instructions into the workflow. The context assembler keeps dossier
content in a typed, length-bounded evidence slot; only an explicit owner action
may promote a conclusion into glossary policy or select a dossier revision for a
later operation.

An external executor may organize its own agents and tools, then submit bounded
conclusions through the owner-scoped HTTP or Translator MCP dossier contract.
Carlina does not pretend that passive translation windows are native research
roles: it owns source scope, immutable artifact persistence, validation,
provenance labels, review state, selection receipts, and operation freezing.
For an initial passive translation, the owner may select one exact dossier
revision at start. Carlina accepts it only when it belongs to the same owner,
project, active source plan, covers the complete plan, passes its retained-key
integrity receipt, and remains explicitly `external_unverified`/`unverified`.
The selected revision and digests are then frozen into the operation config and
bound to the MCP execution request, operation checkpoint, and idempotency
identity before any translation work is queued.

## Glossary and reader annotations

### Shared glossaries across books

Shared glossaries are Translator-owned, private assets with an explicit language
pair, stable concept identities, and immutable revisions. They outlive the project
that supplied their first terms. A project may link one shared glossary revision
and retain local additions and exceptions, or copy selected terms without an
ongoing update relationship. Origins are retained in either case. Source quotes,
occurrence anchors, and reader annotations remain with their source project;
importing a term never fabricates evidence in the destination book.

The glossary dialog separates **This book** from owner-only **Shared glossaries**.
Choose terms from the current book to create an asset or prepare a contribution.
Choose an existing asset to preview a link, copy, or update. Updates compare the
previously adopted choice, the current local choice, and the proposed shared
choice. Changed rows require explicit decisions; local exceptions and human locks
are not silently overwritten. A matching headword is only a candidate match:
contextual senses remain distinct. Shared revisions are advisory publications;
publishing, copying, linking, or accepting a contribution never creates a human
lock. Existing authenticated owner approval in **This book** establishes binding
terminology for a subsequent run.

Import/adoption and unlinking require an idle project and a fresh comparison.
Already-issued packets, operation baselines, target text, and historical receipts
remain unchanged. Unlinking preserves the book's imported terms and their origin.
Changing the active source plan invalidates a comparison; bindings must not be
silently moved to a different source plan. Publication is not synchronization:
each linked book explicitly reviews and adopts a later revision.

For example, Volume I publishes revision 1 and Volume II adopts it. A refinement
made during Volume II becomes a pending contribution with a reason and before/after
values. Reviewing and accepting it publishes revision 2. Volume I may then adopt
that revision, keeping any intentional local exceptions, and use **Check existing
translation** to find candidate passages for contextual editing or selected
retranslation. Glossary adoption itself never rewrites prose. Glossary-divergence
diagnostics are review evidence, not a complete semantic consistency proof.

An author-wide asset is a reusable reference, not a claim that the same spelling
has the same meaning in every work. There is no implicit author/series/collection
inheritance hierarchy. The first implementation supports one linked asset per
project plus independent copies and project-local decisions.

### MCP and passive shared-glossary workflow

The owner-scoped `mcp.translator` surface exposes the same service as the UI:

| Tool suffix after `carlina_translator_shared_glossary_` | Purpose |
| --- | --- |
| `list`, `get` | Discover assets and read a selected immutable revision and pending contributions. Follow pagination. |
| `project` | Read the project's adopted revision, available revision, local terminology, and origin mappings. |
| `create` | Create an independent shared asset from explicitly selected current project concept IDs. |
| `preview` | Compare a selected revision using `link`, `copy`, or `update`; obtain a version-bound preview token. |
| `apply` | Submit explicit `incoming`/`local` decisions against that exact preview. |
| `detach` | Remove the update relationship while preserving project terminology and origins. |
| `contribute` | Freeze selected project refinements and a reason into a pending shared proposal. |
| `review` | Accept a proposal as a new advisory shared revision, or reject it. |

Before starting a translation, list/get the desired glossary, read the destination
project state, and preview the exact revision. Review the returned base/local/incoming
terms, resolve every changed row for a link/update (or select a subset for a copy),
and apply with the returned token and a stable idempotency key. Use the destination
project's concept IDs and revision fences after import, not IDs copied from the
source book. Check the returned authority and applicability. Approving a shared
contribution does not authorize bypassing a human-locked destination term.

During passive translation, continue using `carlina_translator_glossary_get` and
`carlina_translator_glossary_propose` for local working terminology. Save refinements
before submitting a window when practical; newly prepared windows can use them,
but leased/prepared packets and retries keep their frozen terminology. A shared
contribution may be prepared and reviewed during execution, but imports/adoption
wait for an idle boundary. Never restart or recreate a project merely to pick up a
glossary change. After completion, reconcile the shared revision and explicitly
review affected existing prose when requested.

For a contribution, send the selected current local concept IDs, the expected
shared head revision, and a reason explaining where the refinement applies. Read
its proposed changes before accepting. A later shared edit makes an old contribution
stale: reconcile and prepare a fresh proposal instead of repeatedly submitting an
old acceptance. Retirement is an explicit shared proposal; it does not silently
delete a destination project's term. UI and agent requests use owner isolation,
revision/source-plan fences, encrypted provenance and exact retry receipts. Retry
an uncertain mutation with the same key and identical arguments; changed intent
requires a new key. Stale previews must be regenerated and reviewed.

Limits are explicit: at most 2,000 terms per asset or selected mutation, 100 items
per read page, and 2 MiB per retained shared payload. Larger inputs fail instead of
silently dropping terminology. Materialization uses bounded existing glossary
batches inside one atomic action. Cross-owner sharing, automatic propagation,
arbitrary multilingual ontologies, and translation-memory reuse are not part of
this contract.

### Working glossary during translation

Passive document-block translation uses three distinct stages: inspect the source,
optionally research and pin an external dossier, then translate while maintaining
live terminology. Source review/search/fetch tools remain available to the external
executor, which may also use its own research tools. No managed researcher is required.

`carlina_translator_glossary_get` reads/searches paginated current concepts and
revision fences. `carlina_translator_glossary_propose` atomically adds or edits up
to 32 model concepts, including during active translation. It requires the exact
source plan and a stable retry key; edits require a concept ID and expected revision.
The returned items verify the saved delta. Prefer saving changes before submitting
a translation window so subsequent preparation can include them. Repeated unchanged
terms require neither another write nor another dossier.

Canonical source/target terms use dictionary citation forms. Contextual senses
remain distinct; preferred target renderings permit grammatical inflection, while
an explicit exact policy requests fixed spelling. Exact source quotes are resolved
server-side to source-projection offsets, with a one-based occurrence index when
ambiguous and an optional fixed sentence anchor. Observed forms may differ from the
headword. Eligible-span validation excludes protected text; lexical association is
model evidence, not a machine-certified morphological analysis. New usage metadata
is encrypted and receipt-bound, while legacy payload serialization remains unchanged.

Newly prepared windows select bounded relevant model terminology. Already prepared
or leased windows, including retries, reuse their retained lexical snapshot.
Human-locked constraints remain authoritative; a model cannot overwrite or fabricate
a human lock. Saving advice does not rewrite the frozen research baseline or earlier
translations. Completion, cancellation and restart retain project terminology.
The older `translator_get(include_glossary=true)` review projection remains supported.

Adaptive sizing keeps the optional pinned preparatory dossier
separate from the live lemma/sense/forms glossary. Lookup uses a script-aware,
Unicode-canonical (NFKC plus casefold plus whitespace) duplicate guard that
compares current lemmas and senses without changing legacy stored hashes,
profiles, or receipts. Exact source quotes resolve server-side to
canonical-projection offsets. The selected glossary is snapshotted once per
sizing pass and refreshed once at each newly issued boundary; issued scopes are
immutable facts that reject corrupt overlap and order gaps, while only the
unissued tail is repartitioned and shrinks as the glossary grows. A singleton
oversized block is explicit (`oversized_entry`). Validation stays structural,
not punctuation-based, with same-lease preflight feedback. No extra NLP library
or model service is involved.

When final assembly overflows because continuity, precedent or selected memory
adds context, the next unissued-window retry includes that known context in its
fit checks. It must not repeat the cheaper initial planning estimate and select
the same oversized scope. The input-token limit remains unchanged, already-issued
packets stay immutable, and a complete singleton that still cannot fit fails
explicitly rather than discarding required evidence.

Sentence alignment validates exact coverage, nonempty units, complete monotonic N:M
partitions, markup and protected references. Executor-authored linguistic boundaries
are not vetoed by a punctuation heuristic. The deterministic legacy splitter remains
available for its original purposes; stored spans are not rewritten. v8 passive
submissions additionally run packet-local structural preflight and return per-block
errors under the same active lease. This is not full source-integrity validation or
a target-commit receipt; worker validation and activation remain authoritative.

Polling exposes persisted active-target counters in `translation_progress`, distinct
from operation-stage percentages and explicitly not a fresh integrity audit.
`carlina_translator_configure_batches` permits version-fenced budget changes only
at an idle boundary without duplicating the project or replanning source sentences.

The workspace's icon toolbar precedes the project heading and sticks to the
viewport's top edge. It opens preview, glossary, research, sharing, settings,
requests, and export dialogs with accessible labels. Retranslation selection
and pending-block translation also live there; execution inherits the saved
project model and reasoning settings. Opening the
glossary refreshes externally added terms while preserving an unfinished local
draft. Export progress and download readiness remain available from the toolbar.
MCP output remains available when an export follows a completed translation on
the current verified source plan. A newer unfinished translation suppresses it.

The authoritative block glossary already persists concepts, append-only
revisions, human approval, and a compare-and-set active revision for the next
operation. Duplicate terms and stale edits fail closed. Active human-locked
revisions override advisory terminology; model or externally submitted output
never creates a human lock implicitly.

Every accepted analysis or research terminology conclusion must enter this
durable proposal/review path, including bulk external submissions. A custom
prompt may add stylistic instructions, but it is not a substitute for database
glossary rows: reviewers must be able to see where a term came from, edit it,
approve it, retire it, and determine which exact revision an operation used.
Glossary `notes` are private/editorial usage, register, or context guidance for a
term. They are not reader-facing translator's notes and the workspace must not
present the two as though they were one object.

Reader-facing translator annotations are separate, source-linked project state.
The block path persists multiple model or human notes with stable identities and
append-only revisions. The workspace can add, edit, retire, restore, approve,
and re-anchor them; source, target, and whole-block anchors are revalidated when
target truth advances, with stale notes retained as `needs_review`.

The boolean `enable_translator_notes` becomes a versioned annotation profile:

- **None** asks the model for no notes, while existing and human-authored notes
  remain visible and editable;
- **Restrained** permits occasional notes only when a culture-bound term,
  unmarked reference, necessary retained source expression, or material
  ambiguity cannot be handled cleanly in the translation itself; and
- **Study** treats explanatory annotation as part of the deliverable. It invites
  bounded notes for scriptural and rabbinic allusions, unusual technical terms,
  deliberate source-language retention, consequential ambiguities, and editorial
  interventions without turning the translation into a line-by-line commentary.

Existing `false` and `true` values migrate to **None** and **Restrained**. The
profile controls model generation, not whether a human may annotate or whether a
reader may see an existing note. A study profile may return several notes for a
block under explicit per-window count and byte ceilings; it is not constrained by
the current one-note projection.

Passive projects do not select a Carlina-managed translator model in workspace
settings. Their external executor capability and reasoning profile are confirmed
and frozen when the passive operation starts; changing the annotation profile or
batch limits therefore must not require a configured Carlina model.

Every note has a stable project note ID and append-only revisions, an author type
and share-grant/user provenance, active/retired/needs-review state, document
order, optional references to entries in the selected research dossier's
bibliography, and one of these anchors:

- a whole-block anchor for an explanation about the unit as a whole;
- a stable source anchor when the note explains the source rather than one target
  wording; or
- a target-revision anchor consisting of the target revision plus a verified text
  occurrence and bounded surrounding witness.

Visible `anchor_text` alone is never the note's identity. Manual editing or
retranslation revalidates attached notes. A note that no longer attaches becomes
`needs_review`; it is not silently deleted or suppressed. Authorized humans may
add, edit, re-anchor, retire, or restore a note with optimistic concurrency, and
may keep a useful model note while replacing its wording with a human revision.
Active reader notes render in the workspace and in the same semantic note/endnote
structure used by preview and every export format. Authorial source notes and
translator-supplied annotations remain visibly and semantically distinct; a
translator citation must never masquerade as a reference supplied by the source
author.

Added annotations use a separate `[T1]`, `[T2]` sequence in semantic HTML and
EPUB, and every body visibly begins “Translator’s note:”. Imported source-note
labels, bodies, and protected reference identities remain source content. A
translator annotation anchored to source wording is still a translator note.
Only active annotations enter exports; proposed, retired, and stale anchors
remain review state. The bilingual workspace offers hover/focus reading and
click-to-pin note popovers, with Escape, close, and full-note navigation. The
document preview uses the same controller in an opaque sandbox, permitting only
its exact hashed script and retaining local note/backlink navigation.

Reader-note bodies support limited inline Markdown: italics, bold, inline code,
and backslash escapes. Raw bodies and integrity receipts are unchanged; the
derived `body_html` projection and semantic export use the same restricted inline
parser. Only `em`, `strong`, `code`, and line breaks are emitted, with no
attributes, links, images, or raw HTML. Other markup remains literal. The browser
accepts only this inert vocabulary, and popovers also retain safe inline
formatting from source notes. The preview fills the viewport and searches the
whole rendered document, including phrases across inline formatting, with
previous/next navigation and a bounded 2,000-match display.

Export uses a centered modal with queued/building status, elapsed time, bounded
failure messages, and a retained download link. Hiding the dialog does not cancel
the job or start another export when it is reopened. Durable failed, cancelled,
or partial operations override stale queue status; successful delivery still
waits for the prepared artifact. The download is offered before a background
workspace refresh completes. HTML, EPUB, DOCX, and PDF use the same active semantic
reader notes. DOCX normalizes semantic and legacy backlinks to its generated
footnote reference IDs while preserving ordinary internal links and rejecting
unresolved references.

Exported endnote collections use native lists or explicit `list`/`listitem`
wrappers, retaining custom reader-note labels without adding another visible
numbering sequence. Publishing omits deprecated endnote roles and redundant
footnote roles inside those collections; EPUB note types, IDs and links retain
their meaning. Ordinary in-body footnotes retain their footnote role. Historical
source snapshots and accepted targets are not rewritten by this export projection.

Source `note_arrangement` metadata travels through the bounded snapshot and
edition-metadata projection to EPUB export. When chapter/section numbering and
its governing heading level are established, notes whose incoming links belong
to that one unit appear after its final fragment. Shared, unreferenced or
conflicting notes retain their position. Printed labels and return links survive
this placement; ambiguous source relationships are not repaired by export.
Existing snapshots without the extension keep their previous placement.

The export dialog supports translated-edition title, subtitle, authors,
translators, editors, other contributor names and MARC roles, description,
subjects, publisher, publication date, rights, identifiers, series, and volume.
Project target language is authoritative. Source edition publication metadata
and contributor credits remain immutable evidence; they are not silently
attributed to the translated edition. Source title/authors remain defaults.
HTML receives escaped head metadata; EPUB receives the same normalized metadata
through the semantic provider. Body text, structural IDs and note links are
preserved. EPUB retains meaningful body siblings outside the main wrapper,
including source endnotes.

The internal export request binds the original source metadata, provenance, and
resource manifest with canonical-JSON SHA-256 digests (plus the resource count),
rather than duplicating those records in size-limited publication metadata.
The full records remain in the immutable source plan and are verified during
rehydration. Resources needed for rendering still travel as the separately
validated resource manifest; replacing a cover does not change the digest of
the original manifest. Only publication fields enter the metadata projection,
and internal witnesses are not written into the EPUB. Metadata size limits
remain unchanged for all formats.

DOCX maps author/title/language to native document properties and retains
contributor roles and edition fields in custom properties. Repeated DC values
are joined for Word's scalar properties so subjects or multiple translators are
not silently dropped by Pandoc.

Semantic DOCX and PDF exports use a monochrome A4 reading layout with serif
type, 25 mm margins, and page numbers. DOCX uses the immutable Document
Processor reference template, also used by that add-on's individual and project
exports. `scripts/build_document_reference.py` rebuilds it with Pandoc and
python-docx; runtime export never writes into the installed package. Source
illustrations keep their colours.

When the leading source H1 matches the edition title after Unicode, whitespace,
case, and terminal punctuation normalization, DOCX removes only Pandoc's
redundant generated title/author block. The original H1, source prose, bookmarks,
notes, and document properties remain. An unrelated chapter heading or a matching
title later in the text does not trigger removal. Concatenated Document Processor
exports similarly retain an existing source title and its heading hierarchy.

H1 starts a new page. **Start H2 headings on a new page** is an optional DOCX/PDF
export setting, off by default: enable it when H2 represents chapters, letters,
or a preface; leave it off for ordinary subsections. It changes only the export
projection, not stored heading levels or source page boundaries. Empty leading
Word bookmark paragraphs cannot manufacture a blank title page.

PDF is a searchable reading edition rendered in Document Processor's locked
runtime, with heading bookmarks and clickable references. Notes retain their
semantic position as linked endnotes; they are not laid out as Word footnotes.
The renderer receives only exact owned or embedded raster image bytes in an
in-memory resource archive, cannot fetch source URLs, and runs with a bounded
timeout. Missing image resources fail the export. PDF metadata includes title,
authors, description and language; full edition metadata remains available in
HTML/EPUB and DOCX properties. The historical segment-engine export path retains
its existing formats and behavior.

The export dialog can suggest missing title, subtitle, author names, description
and subjects with a selected, configured model. A bounded source packet contains
whitelisted metadata, selected source excerpts and verified translated headings.
The strict response schema permits empty values when evidence is insufficient.
Suggestions fill only blank fields, preserve credits and publication facts, and
remain an unsaved proposal for review in the dialog. No model is called merely
by opening export, and manual editing remains available without a ready model.
MCP exposes the same packet through
`carlina_translator_export_metadata_prepare` so the calling executor authors the
suggestions itself, then saves the merged metadata with the existing versioned
update tool. Preparation and managed generation do not persist metadata.

Owner MCP creation accepts `settings.export_metadata`; the optional
`include_export_metadata` read exposes defaults and their version.
`carlina_translator_export_metadata_update` replaces the complete override set
with optimistic version checking and identical-retry recognition. Active work
blocks changed defaults. Each admitted export freezes its effective metadata in
its request fingerprint, so queue payload drift cannot alter the edition.
UI dialog choices apply to that export. `include_output` continues to expose the
raw verified target projection, not the packaged edition.

Owner-key MCP supports document packaging through `carlina_translator_export`
(`project_id`, `format`: `epub`/`docx`/`html`/`pdf`, and a nonblank `idempotency_key`
of at most 128 characters). It freezes saved metadata, cover, source-plan and
semantic-provider witnesses through the existing export operation and queue;
there is no model call or separate translation execution. Admission retains the
project's single-flight fence. Retry uncertain submissions with the same key and
payload; changed requests conflict rather than replace an earlier export.
The optional strict boolean `start_h2_on_new_page` is valid when true only for
DOCX/PDF. It is frozen in the operation fingerprint when enabled; omitted/false
preserves legacy request identity. The browser sends the same option in its
export request. Document Processor accepts it on individual and project export
routes as the query values `true`/`false` or `1`/`0`.

`carlina_translator_export_get` polls the exact `project_id`/`operation_id`, even
after later project work. The `translator-mcp-export-v1` projection exposes safe
status and returns `ready: false` while queue delivery is pending. Once ready,
it includes filename, media type, byte length and the creation SHA-256 witness.
Metadata reads contain no bytes. Set `include_content: true` for base64 chunks
using byte `offset` and `limit` (1–65,536, default 65,536); follow `next_offset`
until `eof`. `expected_sha256` pins the witness across requests. Verify chunk
checksums and the assembled file's total length and SHA-256 before declaring a
successful download. Reads check regular-file identity, size and stability;
the stored digest is a creation witness, not a fresh whole-file hash on every
chunk. No whole-file size cap is imposed by chunking.

Both tools require `mcp.translator` and resolve the owner before operation or
artifact lookup. Retrieval checks the exact operation's queue result and only
opens its artifact inside its own export directory, rejecting symlinks and
paths outside that directory. It fails closed on platforms without the required
no-follow directory-descriptor primitives. Responses contain no server paths or
raw worker exceptions. These MCP tools are source capabilities; activation on
an existing managed service requires a release and connector rediscovery.

Owners may upload a JPEG/PNG cover in the dialog or import one through
`carlina_translator_cover_import` from the caller's authorized local roots.
Only owner-scoped upload paths are accepted. Inputs are bounded to 10 MiB and
25 megapixels; image metadata is stripped and a JPEG up to 1600 by 2400 pixels
and 700 KiB is pinned by SHA-256. Workers verify the canonical bytes again.
The replacement preserves source evidence and fails on ambiguous cover markup;
an empty override retains the original cover. No remote URL fetching is added.

EPUB exports also offer a physical source PDF page or a static generated cover.
Both use the Document Processor packager. Generated covers use the translated
edition's book details, including translators. The PDF choice uses the source
page's original language and can show a model-suggested physical page. The
per-export choice does not overwrite saved uploaded covers. MCP clients use
`epub_cover_mode=generated` or `epub_cover_mode=pdf_page` with
`epub_cover_page` counted from 1. These options participate in the immutable
request fingerprint; changed retries cannot replace an admitted export.

Translator and asynchronous Document Processor exports prepare an authenticated,
same-origin blob before offering an explicit user-clicked download link. Links
remain available after completion and are revoked when replaced or unmounted.
Job completion, artifact preparation, and a browser's local save are distinct.
Word uses one continuous native footnote sequence, retaining the visible
translator attribution. Repeated references to one source note use a bookmarked
`NOTEREF` field rather than duplicate note bodies. Adjacent references are comma
separated. DOCX removes explicit presentation-marker spans only; an unmarked
leading date, quantity, or citation remains part of the note body.

## Accountless review and sharing

Status: **digest-only Viewer/Reviewer grants are implemented.**

The older plaintext project `share_token` remains a deprecated compatibility
read path for existing links. New links are created and managed only through the
capability-grant workflow below; the workspace never displays or reuses the
legacy token.

The replacement is a set of independently revocable project share grants. Each
grant has a public ID, installation-keyed token digest, owner-supplied label,
role, creation and optional expiry time, last-used metadata, and revocation
state. The raw secret is shown only when the link is created. It must be removed
from ordinary API URLs, referrers, browser history, diagnostics, and stored rows
after initial redemption. Every write is attributed to the grant ID, never to
an invented user account or to the raw token.

A browser share URL carries the high-entropy raw secret in its fragment, never
in its query string. The workspace redeems it once through a rate-limited route
for a short-lived HttpOnly, SameSite capability session and immediately replaces
the fragment-bearing history entry; ordinary requests then use that session
rather than replaying the secret. State-changing requests retain Carlina's
origin/CSRF checks. A non-browser governed HTTP client presents the secret in a
dedicated authorization header and receives the same role/scope checks. Neither
route turns a project share grant into an owner-scoped MCP key.

Standalone review pages load the host's current theme and CSRF fetch helper.
Redemption and session restoration send the host `X-CSRF-Token`; cookie-based
reviewer mutations also send the session's `X-Translator-CSRF`. Neither layer
is exempted. The sharing dialog uses the shared theme's defined button colours,
with both click and Enter submission supported.

The first roles are deliberately narrow:

- **Viewer** can read the bounded source/target workspace, active glossary,
  research conclusions, and active reader annotations, but not unapproved
  terminology/annotation proposals, private provider settings, prompts,
  metrics, receipts, or dormant evidence; and
- **Reviewer** adds version-fenced target corrections, note authoring/editing/
  retirement, glossary proposals, review decisions, and durable retranslation
  requests.

A reviewer link does not change project settings or source structure, manage
shares, build exports, or start billable model/research work. It may request a
new run with a scope and rationale; the owner decides whether to execute it.
Future model-spend authority, if needed, is a separate explicit capability and
is never implied by `Reviewer`. Owner and token-authenticated writes use the same
validation, idempotency, optimistic concurrency, audit, and active-operation
fences.

## Document-native review and model access

Status: **implemented for bounded reads and the initial editorial mutation
surface.**

A governed model client can work on a retained partial or complete
translation without screen-scraping the workspace. One principal-scoped review
service, reached through owner-key Translator MCP or through owner/Reviewer HTTP,
provides bounded contracts to:

- fetch the outline or exact blocks by stable block IDs, document-order range,
  or human-facing paragraph/heading reference;
- search source text, active target text, glossary terms, reader annotations,
  and selected research dossiers, with bounded preceding/following block context;
- return safe source/target editor projections, block role and heading path,
  active target version, active notes, applicable glossary entries, review state,
  and concise provenance without exposing internal hashes, locators, receipts, or
  dormant model packets;
- append a version-fenced target correction, add/edit/re-anchor/retire a note,
  propose a glossary change, or create a scoped retranslation request; and
- paginate and enforce block, byte, and token ceilings rather than returning an
  unbounded private document by accident.

The first search contract is deterministic Unicode-normalized literal/phrase
search with explicit field and block-role filters, stable document-order results,
match spans, a cursor, and caller-selected before/after block counts within a
server ceiling. A request producing more than 10,000 literal matches fails as
too broad instead of allocating an unbounded result set. It does not depend on
embeddings and does not pretend fuzzy or semantic similarity is an exact textual
hit. Semantic retrieval may later be an explicitly different mode with its own
evidence and limits.

Outline and exact fetch select and paginate the requested page before loading
its note, review, glossary, or research projections. Document-wide annotation
search may inspect every block, but calls the scholarly note/review boundary in
chunks of at most 512 verified block IDs. This keeps a 725-block book inside the
same bounded service contract instead of weakening the downstream scope guard.

Read, annotation, target-edit, glossary-proposal, review-decision, and run-
request authority are separate scopes. All mutations require an idempotency key
plus the expected target, note, or glossary version and fail with the latest
safe projection on conflict. No review tool may mutate the immutable source plan
or silently activate a model-generated glossary lock.

Finished projects remain editable. Exact review fetches expose each current
reader note's canonical ID, head `version`, complete body (up to 4,000
characters), safe formatted projection, anchor, and public citations. These
are the same note identities and versions used by the mutation tools; the
older flat `anchor_text` and `author_type` fields remain reading aliases.
Retired notes are omitted and cannot be resurrected by immutable model-note
fallbacks. Annotation search uses the full current body and returns bounded
excerpts. A body-only note revision keeps its persisted target identity and
checks it against the current target; callers need not resubmit the anchor.

The owner-key MCP surface names the read tools
`carlina_translator_review_outline`, `carlina_translator_review_fetch`,
`carlina_translator_review_search`, and
`carlina_translator_review_explain`. Search defaults to literal Unicode
NFKC/casefold matching; bounded regex mode accepts only explicit flags and has
both per-value and whole-document time limits. Review projections expose physical
source page numbers where the immutable snapshot contains PDF page markers, and
`carlina_translator_review_source_pages` renders up to four resolved source pages
through Document Processor's existing bounded image path.

`carlina_translator_qa_diagnostics` runs a deterministic, read-only QA pass over
active targets. It reports structural evidence, citation-key changes, repeated
punctuation, script-aware length anomalies, glossary divergence, joined/split
tokens, source-language leakage and optional Enchant/Hunspell spelling advice.
Findings are paginated, revision-bound, severity-capped and explicitly separate
from target validity and human review decisions. Per-block results are cached by
source plan, active target version, enabled rules, dictionary capability and
glossary fingerprint. Glossary source forms are indexed before block scanning,
so ordinary terminology QA scales with plausible local candidates rather than
every document term. Accepted contextual target variants remain subordinate to
the preferred glossary rendering and may be edited by the owner.

Source now also provides
`carlina_translator_target_update`: it accepts one block candidate, the expected
active target version, and a required idempotency key. Its encrypted,
actor-scoped receipt binds the canonical request and retains the original safe
block projection, so an exact retry returns the same result even after a later
edit while different reuse fails closed. A freshly started external client must
accept the current tool catalog. Target editing and source-plan
correction take the same active-plan pointer lock: the edit is either visible to
the correction's target carry-forward or fails as stale, never reporting success
for a newly superseded plan.

Manual target edits retain verified provider sentence boundaries and split/merge groups
when text changes remain inside the existing sentences and leave sentence
delimiters unchanged. New spans and memberships belong to the new target
revision; their origin is `carried_forward` and they still require review.
Boundary-changing edits use the labelled automatic fallback, which stays labelled
as fallback through subsequent edits. Oversized or expensive text comparisons
also use fallback: input length, cumulative matcher work, and span mapping are
bounded before retaining a pairing. The owner-only
MCP option `restore_parent_alignment=true` explicitly recovers the immediate
parent target's pairing, useful for edits saved by older versions. Recovery
requires exactly one verified parent alignment and a safe text rebase; missing,
ambiguous, corrupt, or unsafe evidence is rejected atomically. The option is
included in the edit's idempotency identity. This does not restore old wording
or approve the translation, and does not alter source-plan correction behavior.

A `whole_block` reader-note anchor is represented publicly by `text: null` and
`occurrence_index: null`; source and target anchors require non-empty bounded
text plus a non-negative occurrence index. Browser and service validation must
branch on the anchor kind rather than rejecting the null whole-block witness.

An explanation request is read-only. Carlina does not retain or expose private
model chain-of-thought: a model answers from the visible source/target, glossary,
annotation, dossier, and concise operation provenance. If those records do not
contain the original reason for a wording choice, the answer must identify its
explanation as a reconstruction rather than inventing historical intent. Adding
a note or changing the translation is a separate explicit mutation requested by
the user.

The required acceptance scenario is intentionally mundane and now covered: a
model searches heading blocks for a retained Hebrew letter, fetches the relevant
source/target range and nearby context, and can explain the section marker from
the visible record. After a separate explicit request, the owner-scoped note
tool adds a versioned reader note without requiring a full export, database
access, or visual-page traversal.

## Scoped retranslation and context refresh

The current workspace and review service freeze one or more exact block IDs, or
an inclusive document-order range resolved against the active source plan. A
retranslation request carries an operation-only custom prompt, annotation
profile, and one of three research choices: reuse an exact saved dossier, run
fresh whole-document research, or await a new externally supplied dossier.
Selection-scoped external research is first recorded as a scoped dossier so its
evidence remains reviewable instead of disappearing inside one model call.

These choices create a new operation and context revision. They never mutate
project defaults, an in-flight baseline, or an already prepared window. Only the
selected targets may advance; every predecessor revision and unselected active
target remains protected. Relevant notes are carried only after anchor
revalidation, otherwise they remain visible as `needs_review`. An accountless
reviewer may create this request, but only the owner executes model work under the
initial role contract.

## Translation memory

### Context retention and aligned segment advice

New runs freeze `translation-context-v2`. Passive projects default to
`translation_context_mode: self_contained`: each batch can include at most three
complete aligned source/target groups from the preceding relevant prose block,
within 6,000 UTF-8 bytes. Headings, navigation and accessibility text do not
replace the preceding passage. A group is never cut to meet the limit.

Choose `retained` when the same conversation already contains the preceding
translation. Its packets omit the continuity excerpt and retain a small
source-plan/block lookup instead. After compaction or transfer to another
translator, `carlina_translator_context_get(project_id, before_block_id,
source_plan_id, max_segments=3)` retrieves that bounded excerpt without changing
the project or an issued packet. The getter checks ownership, current source
plan and block identity and is safe during active work.

`carlina_translator_start(context_mode=...)` overrides the saved policy for one
passive run. `carlina_translator_configure_batches(context_mode=...)` saves it at
an idle boundary using the existing settings-version fence. Managed calls always
use self-contained context because each model call starts afresh. A changed
effective policy starts a new operation; identical idle retries remain
idempotent. Historical frozen runs keep their original contract and bytes.

V2 reuse advice matches source sentences and returns their complete verified
alignment groups, including merged many-source/many-target groups. Query
occurrences point to deduplicated matches. Exact and character-trigram lexical
matches are labelled separately; a lexical score does not establish semantic
equivalence. Limits remain 32 source queries, 64 candidate blocks, two matches
per query, six distinct groups and 8,000 UTF-8 bytes. Continuity and reuse advice
are optional during admission, so repeated context cannot displace writable
source merely to preserve a suggestion.

When a managed run explicitly pins an existing semantic-memory generation, v2
queries natural source sentences rather than a JSON representation of a whole
window. It uses at most six distinct queries, two results per query, six unique
complete memory groups and 8,000 UTF-8 bytes. Query and memory witnesses are
frozen and checked during replay. This does not create or activate memory
automatically or introduce semantic retrieval into passive packets.

### Historical context contracts

Exact repeated source text is handled before semantic memory. Historical windows
receive compact active target precedents for the same Unicode- and
whitespace-normalized source string, plus same-window repetition markers. This
path is embedding-independent and deliberately advisory: it promotes
consistency without preventing a contextually justified variant or rewriting
earlier targets behind the owner's back.

`translation-context-v1` runs also supply bounded sentence precedents in
managed and passive packets. NFC/casefold/whitespace matches preserve punctuation
for exact matching; approximate candidates use character-trigram Dice similarity
of at least 0.85. This is wording similarity, not semantic equivalence. Context
and negation still require model judgment. Matching consults current, verified
target/alignment pairs, never untranslated candidates or stale target revisions.
One source sentence may map to several target segments; many-source groups are
excluded because their individual translations cannot be attributed safely.
At most 32 query sentences, 64 candidate blocks, two matches per query, six
matches total and 8,000 UTF-8 bytes enter this advisory slot. Notes with identical
labels or wording retain separate occurrence identities. Ordinary edits are
visible on the next packet without re-embedding; prepared packets retain their
original evidence and dependency fences.

Note relationship parsing is reused for the exact immutable plan object during
one planning/packet-assembly call, including its nested fit probes. The scope
ends on return or failure; mutable target evidence keeps its normal current-state
checks. Preparation performance and model latency are separate measurements;
fallback token estimates do not establish provider accuracy or semantic
retrieval quality.

The same frozen context policy bundles explicitly linked source-note blocks with
the passage that cites them when the complete bundle fits admission. Each selected
note is writable once, and exports retain original document order. A note outside
the requested scope is never made writable implicitly. Oversized bundles use
bounded source/active-target context in both directions and translate the note
separately. References resolve from canonical source IDs, including EPUB's unique
member-scoped rewrite, never from visible markers or text similarity. Ambiguous
PDF symbolic references remain visibly unlinked with all note bodies preserved.
Retained runs without the new marker keep their original packet/planning policy.

New runs freeze `foreign_language_policy: preserve-passages-v2`. Retained configs
without this marker retain the original v1 instruction bytes. Both policies keep
foreign passages in their original language, including passages inside notes.
The v2 policy distinguishes originals already in the target language from passages
in a third language:

- Use an existing target-language original instead of back-translating its gloss.
  A reading edition renders a redundant inline original/gloss pair once, regardless
  of order or punctuation. Diplomatic/bilingual editions retain their explicit
  editorial policy. Meaningful differences, commentary and wordplay survive.
- Keep a third-language passage in place and translate it directly in a
  translator's note when no author-supplied translation exists and the annotation
  profile permits notes. No separate request is needed; `none` still forbids notes.
- When an author-supplied translated note exists, translate its quotation directly
  from the foreign original, retaining the note's identity and added commentary.
  Identify meaningful discrepancies instead of silently correcting the author.
  Inline third-language glosses likewise use the original as their translation
  source. Do not add a duplicate translated note.

Linked notes are checked before deciding a translation is absent. Required blocks,
protected literals, references and alignment anchors remain mandatory; this is
model/editorial guidance, not permission for a deterministic deduplication pass or
deletion of source notes. Unavailable originals cannot be presented as verified.

The current block path can retrieve context from one pre-existing active vector
generation for the exact owner, project, source plan, and embedding projection.
That capability is not an end-to-end memory product yet:

- normal translation does not derive eligible entries from approved targets;
- it does not build, verify, or activate a replacement generation;
- retrieval has no accepted semantic-similarity threshold;
- it is not cross-document translation memory; and
- a missing or unusable generation normally degrades to no memory context
  without a useful workspace status.

Therefore Carlina must not claim that previous translations are automatically
embedded and reused with high confidence. That becomes true only after the
approved-entry lifecycle, explicit similarity policy, scope, and visible
ready/degraded state pass the rollout gate.

## Failure, retry, and continuation

A provider or normalization failure receives one witnessed same-operation
retry. Exhausting that budget produces an explicit partial result while keeping
all previously activated windows. A later operation freezes and verifies those
active pairs, plans only pending blocks, and protects subsequent human edits.
The owner may instead explicitly retranslate selected active blocks; the
attempt freezes their exact predecessor revisions, and finalization accepts
only the complete predecessor state or the complete new attempt state. A mixed
window, intervening edit, stale pointer, or partial activation still fails
closed.

Unknown provider outcomes, stale operation/source fences, cancellation races,
and staging failures are not replayed as ordinary model retries. Normalization
events retain the exact safe `BlockContractError` code and bounded,
source-free target-validation finding codes. Strict-text output may contain one
otherwise-empty Markdown JSON fence; prose wrappers, multiple objects, malformed
JSON, a response for the wrong frozen writable version, wrong historical
lineage, unknown or duplicated local references, altered fixed literals, and
invalid target structure still fail closed. A malformed response can pause
progress; it cannot corrupt source lineage, erase completed work, or be silently
accepted.

The compact v3/v4 normalizer also tolerates one narrow strict-text echo: a result
may repeat the input-only semantic `role` only when it exactly equals the
source-owned role for that numbered block. The host discards it before candidate
normalization, so no model-authored metadata survives. A changed role, any other
extra field, or any formatting/reference drift still fails closed. This keeps a
semantically and structurally valid translation from being discarded over
redundant source metadata without weakening provenance.

Cancellation is cooperative and durable. A queued operation can stop
immediately; a running operation records `cancel_requested`, prevents later
work from being dispatched, and becomes terminal only at a safe worker
boundary. A provider request already in flight may therefore finish or time out
before the UI reports `cancelled`. The cancellation fence wins any late
finalization race, so late work cannot turn the operation into a successful
completion or activate another window after the owner's stop request. The
workspace keeps cancellation visibly active and blocks conflicting settings or
new work until that terminal state is durable.

The stop route signals the managed task queue before attempting the durable
operation transition. This lets an in-process worker observe cancellation even
when SQLite is temporarily locked by finalization. If that durable write loses
the lock race after the queue accepted the signal, the route reports the
accepted cooperative stop and the worker persists terminal state at its next
safe boundary. Finalization checks that boundary before dependency material,
between blocks, and before every active-pointer change; its nested transaction
rolls back any span/alignment work created after the last boundary.

Within one worker invocation, the conductor retains a value-only verified
evidence witness for the immutable source plan and exact baseline research slot.
Discovery, retry persistence, dispatch, recovery, and finalization can reuse
that witness; attempt receipts and transcripts, active continuity and memory
pointers, operation/cancellation/lease claims, and post-response fences still
read current database state. A worker restart has no witness and therefore
performs full durable rehydration again.

Source-plan correction uses the same principle at a larger granularity:
predecessor pointers, revisions, and alignments are batch-loaded; immutable
rehydration is memoized for that transaction; and all successor version-one
target/alignment pairs are sealed under one dependency lock and savepoint.
Each carried target still receives its own successor-bound revision, alignment,
pair receipt, and correction reuse witness.

Across operations, an exact reuse receipt is durable and restart-safe. Reusing
it still rehydrates the original attempt/artifact/research graph and current
operation scope; it is not a process cache and never transfers the origin
operation's queue, lease, or mutable state.

Ordinary workspace reads batch-load and verify every active target/alignment
pair plus the active target's complete parent lineage. Request-scoped memoization
may reuse only already-verified immutable rows for the same source plan. Dormant
attempt history and superseded revisions remain durable evidence, but scrolling
does not re-audit all of it; those rows fail closed when recovery or another
consumer actually selects them. Pagination therefore changes the rendered page
without hiding an active-pair integrity failure or producing a query-per-row
walk.

## Regression anchors

The following behaviors require focused tests whenever their surfaces change:

- semantic source planning, marginalia exclusion, structure, and protected
  literals;
- exact-token whole-document analysis and complete-block fallback;
- exact cross-operation analysis/research reuse, including secret-only key
  rotation, endpoint and scope-plan drift, ambiguity, tampering, and restart;
- unconditional enabled preliminary research and frozen read-only tool identity;
- witnessed pre-dispatch argument correction with valid sibling-call
  continuation and successful-evidence accounting;
- exact worker loading for frozen research add-on roots and dependency closure;
- externally submitted research dossiers with exact scope/digest/provenance,
  explicit `external_unverified` conclusion labelling, separately scoped cited-
  source receipts, reviewable terminology and annotation proposals, and no
  mutation of an already-frozen operation;
- strict-text closed schemas; exact durable lineage for analysis and historical
  writable v1/v2/v3 windows; and exact local block/reference/field cardinality
  for writable v4 windows, including deterministic restoration of complete
  outer wrappers, without exposing durable lineage in the writable/result
  transport;
- configurable token-aware translation batch targets plus a bounded semantic-
  block ceiling, provider-capacity clamping, required baseline retention, and
  output-aware whole-block packing;
- one-retry/partial/no-replay semantics and crash/reclaim behavior;
- queued and in-flight cancellation, late-result fencing, and visibly active
  `cancel_requested` state;
- active-pair freezing, human-edit protection, pending-only continuation, and
  exact selected-block or cross-page range retranslation with operation-only
  model, reasoning, prompt, annotation, and research choices; predecessor-aware
  idempotency; and no mutation of unselected active targets;
- glossary append/approval/activation conflicts plus bulk external proposal
  ingress that never promotes itself to a human lock;
- migration of the note boolean to None/Restrained/Study, multiple versioned
  reader annotations, explicit add/edit/re-anchor/retire behavior, stale-anchor
  review, and equal preview/export rendering;
- digest-only viewer/reviewer grants, expiry/revocation/audit, redacted guest
  projections, version-fenced reviewer edits, and owner-only model-spend
  authority;
- bounded block/range reads and cross-surface search with before/after context,
  plus scoped model review mutations under separate read/edit/request scopes;
- side-by-side workspace routing, honest attention states, preview, and export,
  including matching source/target typography, formatted inline presentation,
  one editing surface per semantic block regardless of transport-part count,
  quiet immutable literals, and a persisted attested semantic-fragment source;
  and
- memory generation ownership and similarity policy once implemented.

The source and tests override this prose if they disagree. Such disagreement is
a documentation defect to fix in the same change, not permission to preserve a
friendlier product claim.
