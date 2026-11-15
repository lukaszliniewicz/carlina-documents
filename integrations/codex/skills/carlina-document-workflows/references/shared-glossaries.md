# Reuse and refine terminology across books

Shared glossaries are private, owner-scoped Translator assets with a language
pair, stable concept identities, and immutable advisory revisions. They survive
deletion of the project that supplied their terms. Each book keeps its own local
glossary, additions, and exceptions. There is one optional linked asset per
project, plus independent copies; there is no automatic author/series hierarchy.
An identical headword is only a candidate match: check meaning and context.

Use the live `carlina_translator_shared_glossary_*` schemas and returned revision
fences. Discover these tools once; refresh discovery after an upgrade if missing.
Keep asset IDs, shared concept IDs, project-local concept IDs, asset revisions,
binding versions, and preview tokens distinct. Do not copy source-book occurrence
anchors, quotations, or reader annotations as evidence for another book.

## Choose a relationship and adopt terms

All tool suffixes below use `carlina_translator_shared_glossary_`.

1. Use `list` and `get` to inspect an existing asset and pin the exact revision.
   Follow term/proposal pagination. To publish an asset from a book, first read
   its current local glossary, then `create` with its `project_id`, a name,
   explicitly selected local `concept_ids`, and an `idempotency_key`. Creation
   does not itself link or import into any project, including the source book.
2. Read `project(project_id=...)` for the destination's current local terms,
   source-plan fence, binding, and binding version. Choose explicitly:

   | Mode | Result |
   | --- | --- |
   | `link` | Adopt selected choices and pin an asset revision for later reviewed updates. |
   | `copy` | Import a selected incoming subset independently; any existing link stays unchanged. |
   | `update` | Adopt a selected revision of the already linked asset after comparing changes. |

3. Call `preview` with `project_id`, `asset_id`, `revision`, and `mode`. Inspect
   the base, local, and incoming choices. For link/update, decide every changed
   row using `{"id": "<returned-row-id>", "choice": "incoming"}` or `"local"`.
   Copy accepts a selected incoming subset. Preserve intentional exceptions and
   human-locked terms; shared publication does not override them.
4. At an idle project boundary, call `apply` with those same identifiers/mode,
   the exact `preview_token`, decisions, and a stable `idempotency_key`. Preview
   may be prepared while running, but apply may not. If the local glossary,
   source plan, or binding changed, refresh the comparison and review new choices;
   do not bypass a stale-preview error.
5. Use destination-local concept IDs and revision fences returned after import
   for subsequent local edits. Check returned authority and applicability.
   `detach` requires the current binding `expected_version` and an idempotency
   key at an idle boundary; it preserves imported local terms and their origins.

Retry an uncertain mutation with the same key and identical arguments. A changed
decision is a new intent and needs a fresh preview/version as applicable and a
new key. None of these operations creates human approval, changes issued packets,
rewrites translated targets, or automatically adopts later shared revisions.

## Refine in Volume II, then align Volume I

For example, publish Volume I's selected concepts as revision 1. Link Volume II
to revision 1; also link Volume I if it should receive reviewed updates later.
For books by the same author, reuse only relevant senses rather than assuming
every term applies uniformly.

During passive translation, use `carlina_translator_glossary_get` and
`carlina_translator_glossary_propose` for local working terminology as usual.
Save useful refinements before submitting a window when practical: newly prepared
packets may use them, while prepared/leased packets and retries stay frozen.

To propose Volume II's refinements to the shared asset:

1. Read its current local concepts and the shared head revision. Call `contribute`
   with `project_id`, `asset_id`, selected local `concept_ids`,
   `expected_revision`, a reason describing scope/context, and an idempotency key.
   Use `retire_ids` only for intentional shared-term retirements.
2. Read the pending proposal with `get`, including all proposed changes. Use
   `review` with `proposal_id`, the current `expected_revision`, an explicit
   `accept` or `reject` decision, and an idempotency key. Acceptance publishes a
   new immutable advisory revision. If another edit made the proposal stale,
   reconcile and prepare a fresh proposal rather than retrying old acceptance.
3. Contributions and review may occur during translation. Applying the new
   revision to each book waits for that project's idle boundary. For Volume I,
   preview/apply `update` to revision 2 if linked, or choose link/copy explicitly
   if it was never linked. Repeat for other books only within the requested scope.
   Retiring a shared term does not silently delete a destination's local term.
4. If asked to align already-translated Volume I, separately inspect affected
   passages and use the translation review/edit or selected retranslation flow.
   Adoption only changes glossary choices; it does not fix existing prose.
   Glossary-divergence diagnostics identify review candidates, not a complete
   semantic consistency check.

Never restart or recreate a translation project merely to pick up terminology.
Report the shared revision published, each book's adopted revision, retained
exceptions, and whether existing prose was actually reviewed or changed.
