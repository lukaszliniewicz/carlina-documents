# Review and correct a completed extraction

Resolve the document identity and completed status. The newest backend provides
the review tools below. Refresh discovery or reconnect if the connector retains
an older catalog. Do not start another extraction, change processing settings, or route
source corrections through Translator merely to obtain editing tools.

## Inspect with bounded evidence

Start with the aspect the user asked about; for a general check, scan the heading
outline and page boundaries, then investigate suspicious passages.
Also consult the [issue ledger](issues.md) for open or confirmed reports and
stale-context warnings. Record newly found concerns there when they cannot yet
be resolved. Ledger status changes do not apply text corrections.

- `carlina_document_review_outline`: heading levels, hierarchy, text, physical
  pages, and element IDs. Paginate through the outline. Skipped-level and empty
  heading flags are clues, not verdicts; multiple H1 chapters can be legitimate.
  Compare neighboring headings and source evidence before changing levels.
- `carlina_document_review_search`: literal text search, optionally case-sensitive,
  with up to three neighboring text elements before/after each match. Search is
  not regex; `total_matches` counts matching elements, not all occurrences.
  `scope` is `all`, `body`, `headings`, or `notes`; `page_number` filters retained
  physical provenance. Filters restrict hits, not context. `include_html=true`
  adds complete bounded markup or `html_omitted=true`. Results include heading
  ancestry. Use the returned IDs to fetch exact editable HTML.
- `carlina_document_review_fetch`: complete HTML/text for selected IDs or a page
  of elements. Follow `next_offset`. HTML is bounded to 64,000 characters per
  main response and never silently truncated for editing; fetch smaller descendants
  if a container exceeds the budget. `before`/`after` add up to three neighbors
  with separately bounded HTML. The same scope/page filters are available.
- `carlina_document_review_notes`: paginated footnote/endnote bodies, references,
  backlinks, and bounded diagnostic hints. `include_html=true` includes complete
  body markup when it fits; otherwise fetch its element ID. Follow `next_offset`
  and truncation flags. Note placement is not inferred from reference pages.
  Use `scope="notes"` without a physical-page filter to search unknown-page notes.
  `reference_page_numbers` locates incoming markers, not the note body itself.
  Note heading context comes from incoming references (their common ancestry
  when several differ), never the last chapter before collected endnotes.
  `reference_status="unreferenced"` is a factual review state, not proof of a
  missing marker: unmarked biographies can be legitimate source material.
  Invalid references can be inspected; this does not authorize bypassing edit guards.
- `carlina_document_review_boundaries`: text-only by default. Inspect both sides,
  tags/classes, retained page markers, and whether a marker is inside one joined
  element. Sharing a quotation/container does not establish a paragraph join;
  punctuation and `continued` classes are evidence, not conclusive decisions.
  Cover/flap text marked continued may refer to a distant flap, not the next page.
- For an ambiguous boundary, request its optional images using `page_number`;
  with `include_images=true`; this attaches the physical predecessor and current
  page. Use
  `carlina_document_source_pages` for other or nonadjacent evidence: one to four
  physical, one-based PDF pages per call. Inspect the native image blocks.

Use the returned opaque `revision` as `expected_revision` on subsequent review
reads and writes that advertise it; `document_source_pages` takes physical pages,
not a review revision. The revision covers content and the review indexing
contract. Discard old element IDs after a revision change and refresh the relevant
evidence. Physical PDF positions differ from printed page labels. Missing retained
markers do not establish missing source pages, and endnotes may have unknown page
provenance.

## Apply authorized, scoped corrections

For list-heavy review, fetch the complete affected list and its neighboring
page boundary. Check effective `start`, `type`, `reversed` and item `value`
attributes at each level; count direct children, not nested bullets. Compare
suspected resets/jumps with source images before changing them. Preserve
intentional numbering and distinguish container continuation from a split item.
Review tools do not expose a special list-renumbering action: use the advertised
scoped HTML replacement with its revision fence and protected-content checks.

Treat missing accents, replacement characters, mojibake and mixed-alphabet
look-alikes as review clues, not automatic replacements. Search literal Unicode
forms and compare the source image; canonically equivalent combining sequences
can look identical while differing in exact search/replace strings. Keep expected
text and match counts tied to the fetched representation. A dictionary or
ASCII-folded candidate is not proof of the source's spelling.

For a `mermaid-diagram-v1` figure, review code, label bindings and the source
diagram together. Translate/edit visible label paragraphs through their normal
text path; structural corrections require a complete valid figure replacement.
Do not move wording into the literal template or break figure/label IDs. The newest
backend renders this restricted flowchart format to SVG in previews and exports.
Inspect the generated diagram as well as its stored labels; PDF currently
rasterizes it, while HTML, EPUB and DOCX retain SVG.

Completed-document review needs no new OCR job or passive executor profile.
Use `carlina_document_review_apply` with `expected_revision`, a stable
`idempotency_key`, and at most 50 edits:

- `element_id` plus `heading_level` changes an existing H1–H6 without replacing
  its text or children. Do not mechanically relevel the book based on flags.
- `element_id` plus `replacement_html` replaces that element. Fetch its full HTML
  first and preserve everything outside the intended correction.
- Add `end_element_id` to replace a contiguous sibling range, for example to
  correct a split/join. Include intermediate page markers and retained content.
- `element_id` plus `replace_text={"find":"old text","replacement":"new text",
  "expected_matches":1}` replaces a literal case-sensitive string in visible
  DOM text, including across inline formatting. Match whitespace exactly as
  stored, not as normalized in search snippets. It cannot cross or edit anchors,
  page markers, images, hidden content or block boundaries. A mismatched count
  fails the entire batch; scope repeated cleanup to fetched element IDs.
- `element_id`, `delete_element=true`, and `expected_text` removes one element
  whose normalized full visible text exactly matches the guard. Elements with
  protected IDs/resources cannot be deleted; do not strip identifiers to bypass it.

Preserve IDs, page anchors, image sources, and note-reference targets, including
anonymous note links. The tool rejects overlapping edits, invalid/unsafe markup,
and protected-resource loss. It does not rotate/crop images or retarget notes.
Legacy content can be read, but scoped edits require a current semantic snapshot;
do not bypass that guard with direct database or whole-document writes.

For a visually verified missing marker whose note body already exists, use
`carlina_document_repair_note_reference`. Supply `document_id`, the body
`element_id`, existing
`note_element_id`, exact unique `after_text`, printed `label`, `expected_revision`
and stable `idempotency_key`. The default dry run previews one insertion; save
with `dry_run=false`, then read back both the body and note. It cannot cross
protected text or change existing links, and the normal encrypted review undo
can reverse it. Do not add a marker merely to silence an unreferenced-note status.

An optional `dry_run` validates and returns the proposed revision, match counts,
bounded before/after differences and downstream impact flags without saving;
it adds no approval requirement. If a submission result is uncertain, retry the
exact key and payload. A stale revision requires fresh reads and newly scoped
edits. A duplicate receipt confirms an earlier save; its applied revision may
differ from the current document revision. Only the most recent 32 receipts are
retained, so inspect current state rather than blindly retrying an old operation.

Read back the affected passages, headings, or boundaries after saving. The tool
updates canonical HTML, Markdown, and the knowledge source atomically. Saving
marks derived metadata stale; existing Translator source snapshots,
individual extraction responses, and previously generated exports are unchanged.

Repairing extraction list numbering does not automatically update a translation.
Use the explicit [numbering synchronization](translation.md#synchronize-numbering)
workflow for each intended translation project. Its projection preserves the
pinned snapshot and translated wording; do not recreate the project or modify
its database to propagate numbering.

## Undo a correction

`carlina_document_review_history` returns metadata only, newest first. Use the
entry marked `can_undo` with `carlina_document_review_undo`, its `change_id`, the
current `expected_revision`, and a new stable `idempotency_key`. `dry_run=true`
previews restoration; differences are evidence, not standalone editable HTML.
Read back the restored output. Undo republishes Markdown/knowledge and marks
metadata stale; existing exports and Translator snapshots still do not change.

Undo is restricted to the latest unchanged retained correction; it never rebases
over later work. Retention is up to 32 encrypted records and 1 MiB per document,
not permanent version history. Changes whose inverse fragments exceed 256 KiB
are rejected before saving: split them into smaller batches. There is no redo.

## Inspect regenerated exports

Use the app's extraction export controls or `carlina_document_export` for a
completed extraction. Read a fresh revision with `carlina_document_get`, then
supply `document_id`, that `expected_revision`, a stable `idempotency_key`, and
`format="epub"`, `"docx"`, `"pdf"`, `"html"`, or `"md"`. The default is EPUB;
`start_h2_on_new_page=true` is supported for DOCX/PDF. Export makes no model call.
Regenerate after corrections with a fresh revision and a new key; existing
downloads do not update themselves. A same-key retry returns the earlier export,
including after later edits. A queued export rejects text changes before rendering.

Poll its exact returned `operation_id` with `carlina_document_export_get`. When
ready, record the artifact's `sha256` and `byte_length`. For file delivery, set
`include_content=true`, pin `expected_sha256`, retrieve Base64 chunks of at most
65,536 bytes and follow `next_offset` until `eof`. Decode and assemble in order;
verify chunk checksums and the complete file's length and SHA-256. Default
metadata responses contain no file bytes. Reconnect or refresh discovery if the
tools are missing. Report who actually saved the file when your harness cannot
write local files.
Verify the requested formats can actually be retrieved and opened. Check relevant
headings, notes, image orientation/captions, and resource portability; Markdown
image URLs may depend on the originating app. A completed export job alone does
not prove successful delivery or correct rendering.

Report what was scanned, which source pages were visually checked, corrections
applied, export checks, and remaining uncertainty. A quick structural review
cannot certify every word of a whole book.
