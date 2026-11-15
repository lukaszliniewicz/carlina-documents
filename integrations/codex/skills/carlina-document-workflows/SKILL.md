---
name: carlina-document-workflows
description: Extract, review, correct, and translate documents through Carlina MCP. Use for Carlina book/PDF extraction, heading and page-boundary review, scoped corrections, passive execution, recovery, and export inspection; not for ordinary local file reading without Carlina.
---

# Carlina document workflows

This skill targets the newest Carlina backend. Use its tool schemas, validators,
and returned document/execution state as the workflow authority. Discover the
relevant schemas once; if expected tools or fields are missing, refresh discovery
or reconnect to clear a stale catalog. Follow the claimed packet over prose
examples: retained executions can still carry an older frozen contract.

Read `carlina://guide/passive-work` only when passive protocol guidance is needed
and not already supplied in the server's initialization instructions. Skip it
for scoped completed-document review. If the client cannot read MCP resources,
use tool descriptions, packets, and the relevant reference below; do not invent
a resource-reading tool or stop solely because the client lacks one.

## Passive lease context and recovery

For the first claim in each worker/stage, request `response_mode="standard"` and
read the exact leased messages, response schema, and required context before
authoring. Prefer native `response_mode="compact"` for later claims only while
the executing model still has the same instructions in its active context and
their hashes match. A matching hash held by a client, wrapper, or JavaScript
object does not prove that the executing model has seen or retained the content.

After compaction, worker transfer, process restart, a changed instruction hash,
or any uncertainty, recover the current packet with
`carlina_work_get(work_item_id, lease_token)` under that same active lease.
Retrieve the exact omitted instructions, response schema, and any missing
required context using the returned paths; follow pagination for large fields.
Read the recovered material before authoring. Keep the current work item and
lease token: recovery needs no new claim or job restart. A transport wrapper may
reshape or filter packets, but it must forward omission notices, hashes,
retrieval paths, and `request.dispatch.extraction_contract` when present into
the executing model's context. Keeping the summary only in wrapper/script memory
does not satisfy the contract, and a local hash/cache does not prove the model
received those fields.

## Load only the relevant workflow

- Extract or resume document processing: [extraction](references/extraction.md).
- Inspect or correct a completed extraction, heading levels, page joins, or
  exports: [review](references/review.md). This requires neither a new extraction
  job nor a passive executor profile.
- Translate or review a translation: [translation](references/translation.md).
  Read extraction guidance only if the source still needs processing or a new
  passive executor profile needs setup. That reference covers how to drive
  Translator; for how to translate and review well, also load the separate
  `translation` skill when it is installed.
- Reuse or reconcile terminology across volumes, collections, or an author's
  books: [shared glossaries](references/shared-glossaries.md).
- Coordinate several books with delegated execution:
  [collection orchestration](references/orchestration.md). Use nested book
  orchestration only when the framework and governing instructions permit it.
- Capture or review uncertain readings, name spellings and source problems:
  [issue ledger and web corroboration](references/issues.md). Load this during
  extraction and focused post-extraction review.

## Shared constraints

- PDF vision extraction requires the executing model to read the supplied native
  page images and author the transcription and structure directly. Do not use
  embedded PDF text, external OCR, or script-generated text/HTML drafts as a
  substitute unless the user explicitly permits that alternative. Later visual
  review does not make a helper-generated draft a native-vision extraction.
  Helpers may render/crop images, transport packets, validate authored output,
  and package exports. Supplied text catalogs are navigation hints only; see
  [native-vision authoring](references/extraction.md#native-vision-authoring).
- A request to perform passive extraction or translation is sufficient
  authorization. Do not ask for separate executor-profile confirmation. Declare
  the actual executor capabilities and follow the profile guidance in
  [extraction](references/extraction.md#declare-the-executor-profile).
- Resolve the intended document/project and owner-scoped status. Prefer bounded
  outline, search, and fetch for review instead of retrieving an entire book.
- Keep the exact `execution_id` returned by each start or planning operation and
  use it for claims and polls. `claimed: false` is not terminal. Resume durable
  work; do not create replacement documents/projects as a recovery shortcut.
  For a suspended passive PDF execution, follow `job_get.product.recovery` and
  the [recovery procedure](references/extraction.md#recover-the-existing-execution).
- During extraction or translation, submit only the leased completion, respecting
  its messages, schema, and image blocks. Completed-document corrections use the
  validated review/edit tools. Preserve structure, anchors, images, and note links.
- Preserve source-script Unicode, diacritics and meaningful punctuation. Syntax
  restrictions on IDs do not apply to visible text. Distinguish transcription
  from an explicitly requested modernization or transliteration; follow the
  packet's narrow footnote-marker normalization when applicable.
- Preserve printed note labels independently of unique marker/body IDs. Chapter,
  section and page restarts do not create global numbers. Follow the leased
  packet's note-arrangement schema and use explicit links and witnessed scopes;
  ambiguous labels or ownership require review, never a guessed destination.
- Honor the user's execution choice. If asked to extract or translate the
  document yourself, the parent authors those completions, including OCR and
  model boundary decisions where applicable. Researchers may supply evidence
  within the user's instructions; they do not silently become translators.
  Otherwise assign an exact execution and ready work scope to each worker,
  with the parent reconciling completion.
- Never print bearer keys, widen import roots, restart the service, or modify its
  database to recover. Cancel only the exact execution when explicitly abandoned
  or when cancellation is part of an authorized recovery.
- Report what was actually verified. Processing, structural review, visual
  transcription checks, export generation, and successful download are distinct.
