# Translate a completed Carlina document

Translation begins from a completed Document Processor document, never from an ad hoc copy of extracted text.

## Create and plan

1. Confirm the source document is complete and retain its exact `document_id`.
2. Call `carlina_translator_create` with a clear project name, target language, source language (or `Auto-detect`), the document ID, and only settings advertised by the current tool schema. Use passive mode when the current executor will supply model completions.
3. If creation returns an ingestion/planning execution, poll that exact execution until Carlina reports that deterministic semantic-block planning is complete. Do not start translation against an unfinished plan.

Use the live typed creation schema: annotation profiles are `none`, `restrained`,
and `study`, and translation-unit modes are `block` and `aligned_sentences`.
Inspect a reported structural-limit error before adjusting model budgets: a
source-planning limit is not a translation-window token limit. An unsuccessful
creation must be reconciled with project/job discovery before retrying.

Respect the requested `translation_unit_mode` and `annotation_profile`. In
`aligned_sentences` mode, Carlina supplies the source sentence addresses before
translation; return complete blocks and their natural N:M sentence alignment,
not one result per source sentence. `study` permits several useful reader notes
per block. Budget the executor's output for the translation, alignment, and
notes together; do not advertise a larger capacity than the actual executor.

Passive translation does not run Carlina-managed preliminary analysis or
research. Save externally prepared conclusions through research dossiers when
needed; a custom prompt or the agent's conversation memory is not a persisted
glossary or research record. Retain the `revision_id` returned by
`carlina_translator_research_dossier_submit`. Saving a dossier alone does not
include its conclusions in a translation run.

This reference covers how to drive Translator. How to translate well is the
subject of the separate `translation` skill. When it is installed, load it and
its `references/carlina.md` before creating the project. It sets out the
preparation a passive run needs before the first window (a read of the document,
standing decisions saved as a pinned dossier, harvested terms saved as glossary
entries), what to check in each window, where to keep doubts that the response
contract has no field for, and what QA diagnostics do not cover. On how Carlina
behaves, a tool schema or claimed packet wins over this reference, and this
reference wins over that skill. If the skill is not installed, continue without
it.

## Start and execute

For terminology shared with another volume or book, follow
[shared glossaries](shared-glossaries.md) before starting. Adopt the selected
revision at an idle boundary. Later shared publications do not change the run's
baseline or issued packets; local advisory edits follow the rules below.

Declare the real executor profile as described in the
[extraction workflow](extraction.md#declare-the-executor-profile), then call
`carlina_translator_start` with the exact project ID, passive mode, and that
profile. The user's request authorizes passive execution; no separate
confirmation is needed. Reuse an unchanged profile after an interruption.
Record the new translation execution ID; it is distinct from extraction and
planning IDs.

Choose the context policy separately from the executor profile. For a continuing
conversation that already contains the preceding translation, pass
`context_mode="retained"` to start. For a fresh worker or conversation, use
`self_contained` (the default), which includes a bounded preceding excerpt.
The saved project policy can be changed at an idle boundary with
`carlina_translator_configure_batches`, using its settings-version fence.
Managed calls always receive fresh context. Do not infer retention from model
capacity, tools or reasoning capabilities.

Retained-mode packets include `continuity_lookup` with the source-plan and next
block identity. After compaction or transfer, call
`carlina_translator_context_get` with that lookup to recover at most three
complete aligned groups. Request it only when preceding context is missing;
there is no need to fetch it after every submission. The getter is read-only
and can be used while the run is active. Historical issued packets keep their
original context contract; a new policy applies to a new run.

To use saved research, pass that exact ID as `research_dossier_revision_id`
to the start call. Use the intended project's source scope and let the server
validate the pin; do not guess an ID or silently drop a rejected pin. The run's
research baseline is frozen. Later terminology suggestions remain distinct from
that baseline and do not replace it during active work.

Drain translation work through `carlina_work_claim(execution_id=...)`, renew, release, and submit exactly as for extraction. Respect each immutable block window and its structure, terminology, wrapper, notes, tool, and output-schema requirements. Do not merge windows, translate unrequested blocks, or write target rows directly.

In retained mode, remember `projection.context_manifest.entries` hashes only
while their content is still available. Pass those as `known_context` on later
claim/get calls, and pass `known_target_revisions` only for precedent targets
actually retained. Clear both after compaction or transfer unless independently
preserved. An omitted descriptor is recoverable through full `work_get` or its
exact request path. Never infer acknowledgement from a worker name. Source
wording scores and changed-token/negation/modal/number flags are lexical advice,
not verification of meaning; inspect the complete aligned group before reuse.

New aligned packets use v9 source sentence locators into the single compact
`content` or named field value. Their half-open offsets count Unicode characters
in the actual string values, not JSON escapes. Content offsets precede entity
decoding; named fields are already plain text. Read each ordered span and translate the resulting
sentence; do not reproduce source descriptors in the completion. Saved older
packets retain their supplied contract.

Consume a JSON tool's `structuredContent` once. Use text content only as a
compatibility fallback; do not feed both copies of the same result into context.

After submission, claim the next item with a bounded wait rather than polling
the job after every block. `claimed=false` still requires a terminal job check.
Inspect job state on errors, stalled progress and completion; schema acceptance
alone remains distinct from target activation. Compact window budgets expose
actual block counts and frozen input/output limits for diagnosing small batches.

List numbers are structural source attributes, not translated prose. Preserve
their protected representation and do not invent a new numbering sequence.
When the source contains `mermaid-diagram-v1`, translate only the label/caption
text exposed as writable slots. Mermaid code, node IDs, placeholders and label
IDs remain literal/structural evidence. Keep all source-script characters in
protected quotations and literals; identifier syntax is not a reason to strip
diacritics from translated wording.

Later Document Processor corrections do not update this project's pinned
snapshot. Use [numbering synchronization](#synchronize-numbering) for numbering-only
repairs. Do not recreate a finished translation or overwrite translated text to
propagate them.

New packets can append explicitly linked source notes to the passage that cites
them. Follow packet-local block numbers, including these note bodies, and return
each exactly once. Export still restores the original document order. Repeated
references to one actual target share its translation; separate notes remain
separate even when both display `*` or contain identical text. Never invent a
relationship from a marker, filename fragment without scope, or similarity.
Read bounded `linked_notes` context before translating a passage or note; outside
excerpts are reference material, not additional output blocks.

Preserve quotations and passages in languages other than the source language
in their original language, in both body text and source notes. Translate the
surrounding source-language prose. Distinguish three cases by meaning, not by
brackets or typography:

- **Already in the target language:** use the original wording rather than a
  back-translation of the author's gloss. In a reading edition, render a
  redundant inline original/gloss pair once, in either order, including pairs
  separated by commas, dashes or sentences. Preserve additions, interpretations,
  significant differences, wordplay and discussion of the words themselves.
  Do not silently modernize quotations or apply this condensation to an
  explicitly diplomatic or bilingual edition.
- **In neither source nor target language, with no author-supplied translation:**
  retain the passage and supply a direct translation in a translator's note
  when the annotation profile permits notes; no separate request is needed.
  A no-notes profile still forbids new notes or an inline substitute.
- **An author-supplied translated note exists:** translate that note's quotation
  directly from the foreign original, not through the intermediary translation.
  Preserve the source note's identity, references and additional commentary;
  identify meaningful discrepancies rather than silently correcting the author.
  Do not add a duplicate translator's translation. For an inline gloss of a
  third-language passage, likewise translate directly from the original while
  retaining both the foreign passage and its target-language gloss.

Check linked notes before deciding a translation is absent. Keep all required
blocks, protected literals, references and alignment anchors; a separate source
note must not be deleted as redundant. Do not reconstruct an unavailable original
or claim a direct translation without it. Follow a frozen packet's policy when
executing retained work; these rules govern new v2-policy runs and authorized
editorial corrections, not silent changes to issued work.

Sentence precedents provide exact or approximate wording matches from current
translations in this project. They work without an embedding provider in managed
and passive runs. Check context and negation; approximate similarity is neither
semantic equivalence nor permission to copy automatically. Semantic embedding
generation is not yet maintained by ordinary translation, so do not assume
cross-document or cross-language retrieval has checked every quotation.
New context-v2 advice returns complete aligned groups, including many-source
groups, and maps each query occurrence to deduplicated matches. Treat all group
text as the suggested unit; do not attribute its full target to one source
sentence. Optional advice may be reduced to admit more writable source.

Submission acceptance is not target activation. Retain the exact authored
response and submission identity until the corresponding targets are active.
Read long packets in bounded portions and account for every requested block;
tool-output truncation is not permission to infer missing source text. After an
empty claim, inspect that execution's status and honor its retry guidance rather
than repeatedly reading the whole book or busy-polling. A queued finalizer may
still be validating and publishing the previous window.

Do not make editorial target edits while any project operation is active, even
if its targets are already visible. Wait for a terminal operation, then use the
version-fenced review tools. `target-active-operation` is a retry-after-terminal
conflict, not a stale target version. Keep the revision-integrity guard intact.
After a terminal failure, reuse an authored response only after comparing the
new writable packet with its original scope, source, terminology and structure;
never reuse merely because the block count matches.

Optional workers may process independently claimable translation windows concurrently, but every worker must use the same exact translation execution ID. Keep research or terminology phases distinct when their workload roles differ; submitted external citations remain unverified unless they came through a separately governed Carlina tool.

## Maintain the live glossary

The research dossier and glossary have different lifecycles. A dossier supplies
preparatory research at an explicitly pinned revision. The working glossary is
project-level terminology that may evolve during translation without restarting.
It can incorporate explicitly adopted or independently copied shared terms;
[shared glossaries](shared-glossaries.md) explains cross-book reuse and how to
contribute refinements back without automatic synchronization.
Neither is a substitute for reader annotations.

Before starting, inspect metadata, headings and representative passages with the
Document Processor review tools. After planning, use Translator outline/search/fetch
for stable block IDs and sentence anchors. Research consequential questions with
other available tools when useful. Record evidence and uncertainty honestly in an
optional dossier; a source-only brief needs no invented web citations.

Use `carlina_translator_glossary_get(project_id, query=..., offset=0, limit=50)`
for relevant concepts, observed forms and current revision fences. Later packets
include a bounded selection of current advisory terms; do not read the complete
book or glossary on every turn. The older `translator_get(include_glossary=true)`
projection remains available for the review interface.

Save new or revised terminology with `carlina_translator_glossary_propose`, ideally
**before submitting the current translation** so the next packet can include it.
Only send changed concepts. The result contains saved items and revision IDs, so
an immediate second full read is unnecessary. Retry an uncertain write using the
same idempotency key and identical payload, not a newly invented key.

A minimal addition is:

```json
{
  "project_id": "the current project ID",
  "source_plan_id": "the current source plan ID",
  "idempotency_key": "window-12-terminology-1",
  "entries": [{
    "source_term": "Gemeinde",
    "target_term": "congregation",
    "sense": "religious body",
    "notes": "Distinguish the civil sense, municipality.",
    "match_policy": "preferred",
    "source_forms": ["Gemeinde", "Gemeinden"],
    "occurrences": [{
      "block_id": "an actual block ID from review_fetch",
      "quote": "Gemeinden"
    }]
  }]
}
```

Use canonical dictionary/citation forms, not a universal nominative-singular
rule. `preferred` is lexical guidance: inflect grammatically in the translation.
`exact` requests fixed spelling for exceptional names or strings, not mechanical
substitution. Separate concepts by contextual `sense`; do not automatically equate
compounds with a substring. Associating an observed inflection is a model claim,
not a morphological proof supplied by Carlina.

The host resolves exact quotes against a block's immutable source projection and
validates translatable spans and optional sentence anchors. Repeated quotes need
a one-based `occurrence_index`. Packet-local `n` values are not persistent block
IDs. Do not count HTML offsets or guess an address.

For edits, read the concept first and include its `concept_id` and
`expected_revision`, with all intended lexical fields. These fields replace the
old revision; omitted `source_forms` retain previous forms, while a supplied list
replaces them. Anchored quotes are added to the known forms. Historical revisions
and their occurrence evidence remain unchanged.

Model entries are immediately usable **advice**, not fabricated human approval.
Human-locked mappings remain authoritative. An already prepared/leased packet and
its retries keep their exact glossary snapshot; a new edit applies only to later
newly prepared packets. Never alter a frozen request, add glossary keys to the
translation response, or create a new research dossier for each terminology delta.

## Validation, progress and efficient recovery

The executor supplies natural sentence/fragment boundaries with `<sb/>` and a
complete N:M alignment. Carlina checks nonempty units, exact coverage, schema,
markup and protected references, not agreement with a punctuation heuristic.
URLs, initials, note labels and quoted asides need no artificial extra boundaries.

New v8/v9 submissions receive packet-local preflight. A rejected submission includes
safe per-block codes and leaves the lease available for correction. `dry_run=true`
checks without recording acceptance. Schema/preflight acceptance is still not
proof of target activation: poll the operation and check `translation_progress`
for persisted active-target counts. The operation's 50% preparation baseline does
not mean half the book has been translated.

To change token/block budgets, first reach an idle boundary, read
`batch_settings.version` and call `carlina_translator_configure_batches`, then
resume the same project. Source anchors and completed targets are preserved;
do not create another project merely to tune batching.

Glossary notes are editorial guidance. Reader notes explain the text to its
readers and belong only in the packet's annotation fields. Preserve source
footnote markers and wrappers exactly; do not duplicate source notes as new
translator notes. Respect the selected annotation profile rather than filling
a note quota. Anchor notes to the actual translated phrase and state material
textual uncertainty candidly.

Reader-note bodies support limited inline Markdown: `*italics*`, `**bold**`,
and inline code delimited by backticks. Keep anchors literal. Escape formatting
markers when intended literally; do not emit HTML or Markdown links in model
notes. Stored bodies retain the Markdown, while `body_html` is a derived reading
projection. Never submit that projection as the note body.

A finished project remains editable. Use `carlina_translator_review_fetch` with
explicit block IDs or an inclusive order range to inspect translations and
notes, then `carlina_translator_target_update` or the `reader_note_create`,
`reader_note_revise`, and `reader_note_set_state` tools with the returned version
and an idempotency key. Read back the saved revision and regenerate exports
after corrections; completion is not a reason to recreate or rerun the project.
Inspect sentence groups as well as text after target edits. Local wording
changes preserve existing group membership when their boundaries survive;
boundary-changing edits may need new alignment review. Where an older manual
edit replaced a correct pairing with positional fallback, the advertised
`restore_parent_alignment=true` option can recover the immediate parent
revision's pairing through a fresh version-fenced target update. Use it only
after verifying that the parent is the intended reference; unsafe or ambiguous
recovery fails rather than guessing. Do not restore old wording or write the
database directly to repair alignment.

For a targeted sentence edit, use `carlina_translator_target_update` with
`sentence_edit` instead of `candidate`, retaining `expected_active_version` and
the same idempotency key for retries. Supported objects are:

- `{"action":"replace","sentence_order":1,"text":"Edited sentence."}` preserves
  its existing segment even if punctuation changes.
- `{"action":"split","sentence_order":1,"text":"First. Second.","offset":7}`
  splits at a Unicode character offset in trimmed text; both parts must be
  nonempty. Spaces at the split stay with the preceding segment.
- `{"action":"merge_next","sentence_order":1}` joins that segment and the next
  without changing wording; cross-group merges combine their source membership.

Do not combine `sentence_edit` with `candidate` or `restore_parent_alignment`.
Use the current projection's sentence order, never an inferred punctuation
count. Immutable literals and markup remain protected. Read back the new text
and pairing before regenerating exports.
Use the canonical note `id` and current `version` from `reader_notes`. Its `body`
is complete editable text, not a search excerpt. Omit `anchor` for a body-only
revision. To change the attachment, note writes use `kind: "block"` for a whole
block, or `kind: "source"`/`"target"` with `anchor_text` and `occurrence_index`;
target attachments also use the block's current `target.active_version` as
`target_version`. Read projections instead label whole blocks `whole_block`
and expose the phrase as `anchor.text`. A retired note disappears from current
review results; its mutation response supplies the version needed to restore it.

## Validate and retrieve

### Translated-edition metadata and covers

Discover these tools in the connected server before using them. Creation may
include `settings.export_metadata`. Before exporting a finished translation,
call `carlina_translator_export_metadata_prepare(project_id)`. Author suggestions
yourself using its bounded source packet, translated headings, instructions and
response schema. Fill only empty `editable_fields`: title, subtitle, known author
names, description and subjects. Keep uncertain fields empty and do not imply a
whole-book analysis from selected excerpts. Merge these suggestions into the
returned `metadata`, retaining all existing credits and edition facts, then call
`carlina_translator_export_metadata_update` with `expected_version`. The update
is a full replacement: merge retained fields deliberately before submitting,
and preserve the same expected version and payload for an uncertain retry.
Updates are refused during active work; identical retries remain safe.

Supported fields describe the translated edition: `title`, `subtitle`, `authors`,
`translators`, `editors`, `other_contributors` (`name` and a lowercase three-letter
MARC `role`), `description`, `subjects`, `publisher`, `publication_date` (`YYYY`,
`YYYY-MM`, or `YYYY-MM-DD`), `rights`, `identifiers` (`scheme`: `ISBN`, `DOI`,
`URI`, or `OTHER`, plus `value`), `journal_or_series`, and `volume`.
Do not invent a translator credit, rights statement, ISBN, or other
publication facts. Target language comes from the project. Original edition
metadata remains separate and is not silently reused as the new edition's
publication metadata. Empty optional strings/lists clear those overrides;
omitting title/authors permits source defaults. A supplied title must be nonempty;
`{}` clears all saved overrides. The complete metadata object is bounded to
8 KiB of serialized UTF-8 JSON, even when individual fields fit their limits.

For a replacement cover, call `carlina_translator_cover_import` with the exact
project and a JPEG/PNG path inside the caller's already authorized import roots.
Use its returned `cover_upload_path` in the metadata update. Do not widen import
roots or construct another owner's upload path. The server bounds the upload,
strips image metadata, resizes it, and pins the resulting JPEG by content hash.
An empty cover path restores use of the source cover.

`include_output=true` retrieves the verified target projection; it does not
apply edition metadata or package an export. Verify the actual requested
HTML/EPUB/DOCX/PDF artifact separately, including credits, language, cover, all note
bodies, references, and backlinks. Call
`carlina_translator_export(project_id, format="epub", idempotency_key=...)` with
a stable key (at most 128 characters). It uses the saved metadata/cover and
starts the existing exporter without a model call. Poll the returned exact
`operation_id` using
`carlina_translator_export_get(project_id=..., operation_id=...)`, not the latest
project job. Include both IDs on every status and content request.
Once `ready` is true, retrieve `include_content=true` chunks using `offset` and
`limit` (at most 65,536 bytes). Pin `expected_sha256` to the artifact witness,
decode `content_base64`, check `chunk_sha256`, and follow `next_offset` until
`eof`. Verify the local assembled file's total length and SHA-256 before saying
it was downloaded. Metadata-only responses do not contain the file.
Treat chunk offsets and hashes as bytes. Assemble decoded bytes before decoding
an HTML/text artifact as UTF-8; a chunk may end inside a multibyte character.

Refresh discovery or reconnect if the export tools are missing from a cached
catalog; metadata/cover tools alone do not generate or download an artifact.
The app's export controls are an alternative. Browser export fetches the artifact first and
offers an explicit download link: click it and verify a local file before saying
it was saved. A completed job or visible link alone proves neither download nor
file integrity.

Poll `carlina_job_get` for the translation execution until terminal. A validator
rejection is feedback for that unit, not permission to bypass the validator or
recreate the project. After success, read compact project state and saved terms
with `carlina_translator_get(project_id, include_glossary=true)`, leaving
`include_output` off unless full-artifact inspection is needed. Verify targets
and requested reader notes with bounded `carlina_translator_review_fetch` calls.
Page through the requested review scope; label sampled checks as samples. Use
`carlina_translator_qa_diagnostics` for the deterministic whole-document anomaly
pass. Its default grouped view omits excerpts: inspect repeated-token groups with
`group_id`, then page every occurrence. Use `group_by="finding"` and
`include_excerpts=true` when individual evidence is needed. Use `carlina_translator_review_source_pages`
when a finding needs comparison with the original PDF image. QA findings are
review evidence, not automatic invalidation or approval. Regex review search is
bounded and should be used only when literal search cannot express the pattern.

After actually checking a block or specified source anchors, record the kind of
coverage with `carlina_translator_review_coverage_record`: `structural`,
`target_reading` or `bilingual`. Read for `target_reading` with
`carlina_translator_review_fetch(view="target_only")`, which withholds the source.
Supply current target versions and a stable key;
agent work uses `reviewer_kind="agent"`. A sample covers only its selected
anchors, not the book. Coverage is distinct from approval and automatically
becomes historical after prose changes. Use bounded coverage_get pages to report
what remains. For an export requiring complete review, supply the relevant
`review_requirement`; `none` is a draft. Export receipts bind the checked target
revisions and witnesses. Do not claim whole-book bilingual review from QA,
heading/note review or a sample.

Reconcile completion with the execution and accepted window scope, then verify
target activation in the reviewed records. `translator_get` does not provide
completed semantic-block counts; legacy segment counts, glossary size, and HTML
length are not substitutes. Report the actual saved glossary count and
distinguish suggestions from locked terms. An empty glossary after a
terminology-rich book needs investigation, not a claim that it was preserved.

Verify the requested exports through Carlina, preserving heading/link structure,
source notes, and translator-note attribution. Check reference/body parity in
each requested format; a completed queue job alone does not prove a readable
or complete document.

If interrupted, resume the exact nonterminal execution. If an execution is
terminal partial/failed, inspect the existing project and retained active
targets before starting a new continuation operation on that same project.
Confirm that the next packet contains the remaining scope; do not retranslate
already accepted blocks or recreate the project as a recovery shortcut.
If the user abandons the translation, cancel that execution so unfinished work
is fenced; do not delete the completed source document, translations, or glossary.

## Synchronize numbering

After verifying and saving a numbering correction in Document Processor, call
`carlina_translator_numbering_preview` for the intended translation project.
It compares the pinned source with the current extraction, reports matching
lists and affected blocks, and supplies a `preview_token`. This works before,
during partial completion, and after translation; applying changes requires
that the project's active operation has finished.

Review each candidate against the corrected source. Call
`carlina_translator_numbering_apply` with that token, `selected_ids` copied from
the chosen `changes[].id` values, and a stable `idempotency_key`. These are preview
change identities, not HTML element IDs. Only `ol[start,type,reversed]` and `li[value]`
can propagate. Changed wording, nesting, IDs or ambiguous repeated lists require
manual review; do not bypass a conflict by recreating the project. Each
translation has its own selection and revision.

The encrypted overlay changes source preview and translation preview/export
numbering without modifying the pinned snapshot, translated text, alignments,
notes or target revisions. With browser access, inspect the full source numbering
preview and translation preview, then regenerate the requested exports. MCP-only
review uses the comparison report and generated translation exports; it does not
provide a rendered source-preview tool. Report that visual-review limit when it
applies. Already downloaded files do not change. The compact block editor retains
the pinned source representation; use the full source numbering preview to check
the effective list counters.

For an uncertain submission, retry the identical key and payload. A stale token
requires a fresh comparison and selection. To reverse a synchronization, fetch
a fresh preview and use `carlina_translator_numbering_undo` with its token and a
new stable key. Repeated undo walks earlier synchronizations. A nonempty overlay
blocks a source-plan replacement; undo the overlay before an independently
authorized structural source correction. The bounded history can reject further
writes when full; report that limit instead of editing stored state.
