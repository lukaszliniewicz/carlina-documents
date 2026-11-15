# Coordinate a collection of books

Use this mode for an authorized collection task when the framework supports
delegation. The collection coordinator owns the user's intent, shared choices,
resource budget and final acceptance. A book orchestrator owns execution and
recovery for exactly one assigned book within that contract. Leaf workers author
exact leased completions. Delegating execution coordination does not delegate
authority to change language, editorial policy, output scope or permissions.

Honor instructions to do the work yourself and any framework rule reserving all
orchestration to the parent. In those environments the parent schedules separate
book lanes directly and delegates only permitted leaf work. Never invent a Task
tool or recursively launch another harness to evade a restriction.

## Establish ownership and limits

Before dispatch, resolve each book's document/project identity and current state.
Give exactly one orchestrator ownership of each document/project and execution.
An already-running execution must be resumed by its owner, not started again.
Keep a compact collection register: book, document/project ID, execution ID,
child-session handle, assigned concurrency quota, current checkpoint and result.
Server state is the progress authority; a chat summary is not a durable checkpoint.
The book assignment is an execution contract, not server-side isolation between
books accessible to the same owner.

Choose a collection-wide cap for simultaneous model work, covering metadata,
page batches and translation windows across ALL levels. Also limit active books.
Begin with one active book when capacity is unknown. Two books is a conservative
starting point for remote inference only when observed host memory, image
preparation, provider limits and transport capacity permit it. Local GPU inference
should start with one model task unless measured capacity demonstrates safe
concurrency. CPU count alone is not an inference or memory budget.

Assign fixed quotas whose sum never exceeds the collection cap. Start each book
with one model-work slot; a metadata completion consumes that slot just like a
page worker. A child cannot expand its quota or dispatch other books. Increase
the collection cap only after representative work shows headroom; reduce it for
memory pressure, queue growth, rate limits or transport failures. Prefer finishing
active books before admitting more. Do not modify global server worker settings
as a shortcut to collection scheduling.

## Dispatch one book orchestrator

Verify that the actual child role can access the required Carlina tools, load
this skill, dispatch the intended leaf role, and report resumable child handles.
Framework support on paper is insufficient if the active profile denies nesting.
The dispatch packet must include:

- Native web policy (allowed or denied), inherited from the user's scope and
  privacy constraints. Pass it to page workers; it is not a Carlina MCP setting.
  Public name corroboration is optional and must follow the issue-ledger rules.

- One book's exact document/project and execution IDs, or explicit authority to
  create/start that book when no execution exists. Reconcile uncertain starts
  before retrying.
- Requested stages and final artifacts, languages, editorial choices and source
  page scope; identify what the user has already authorized.
- The actual executor profile and task authorization, batch plan, model/image
  capacity and transport limits. Account for every model that authors completions;
  a different child's capacity must not be inferred from its parent.
- The assigned concurrency quota, permitted leaf role and review responsibilities.
  If no child is permitted to perform a required visual review, return that precise
  checkpoint to the collection coordinator.
- Existing child handles/checkpoints, stop conditions and the required compact
  handoff. Keep credentials, lease tokens and full book text out of summaries.

The book orchestrator loads the relevant extraction/translation reference and
owns metadata stages, readiness checks, scheduling, validation and recovery for
that book. Check `carlina_job_get` before dispatching a page worker: metadata or
local preparation may still be running. For sequential plans, permit only one
claimant for the ready batch. For concurrent plans, assign disjoint exact batches
within the granted quota. List-heavy and continuous-prose books normally use
sequential page batches even while DIFFERENT books run in parallel.

Give a leaf worker one execution, exact ready batch/work scope and unique worker
ID. The leaf owns its lease and renewal, reads all packet evidence, submits a
complete result, then stops. It cannot spawn more workers or start another book.
The book orchestrator must not claim the same work while its leaf is active.
An accepted submission still requires the orchestrator to check product
validation and the durable checkpoint before scheduling dependent work.

## OpenCode v2 integration

Use the custom `carlina-book-orchestrator` role for one book and `carlina-worker`
for one extraction batch. The collection coordinator uses the `subagent` tool to
dispatch books; a book uses that tool only for its permitted worker. Do not use
the built-in general role as evidence that recursive delegation is enabled.

V2 profiles use ordered `permissions` rules with `action`, `resource` and `effect`.
Grant the book role `subagent` only for `carlina-worker`; deny delegation in the
leaf. Custom children have their own permissions, so restrict every role rather
than assuming it inherits the parent's restrictions. The worker needs only
status and claim/get/renew/release/submit tools, plus native websearch/webfetch
when public corroboration is authorized. Static web permission is not a per-task
authorization fence: the dispatch policy must also be honored. The book additionally needs its
workflow skill and document/translation/job tools. It cannot write downloaded
files; hand artifact saving and checksum verification back to a capable parent.

Two delegation levels must be enabled. The V2 migration guide specifies
`experimental.subagent_depth: 2`, but configuration names and defaults vary by
version. Check the installed runtime and verify both child levels with a small
synthetic canary before running a collection. Do not infer nesting from a parsed
config alone. Retain the returned `sessionID`; the V2 `subagent` tool can resume
that child. `background: true` permits asynchronous dispatch, but does not enforce
the collection cap: the coordinator must track all active book and leaf work.

See the official [V2 tools](https://opencode.ai/v2/docs/tools/),
[permissions](https://opencode.ai/v2/docs/permissions/), and
[migration guide](https://opencode.ai/v2/docs/migrate-v1/). If the installed
framework lacks the required depth, use direct parent-owned book lanes.

## Recover and reconcile

Wait through the framework's supported completion/status mechanism, preserving
returned child handles. A child timeout or lost response does not mean its work
failed: inspect that exact Carlina execution and active leases before resuming or
replacing it. Resume the existing child session when supported and appropriate;
otherwise give a fresh child the bounded contract and require it to read current
packets. It cannot inherit prompt content merely from a parent's hash or summary.

Do not start a second book orchestrator until the previous one has stopped and
its ownership/lease state is reconciled. A book failure should be reported without
cancelling unrelated books. Cancel only exact executions within the user's
abandonment/recovery authorization. Never repair coordination by editing the
database, re-creating a translation or changing shared provider accounts.

For a suspended PDF extraction, give the book owner the existing execution ID
and follow [same-execution recovery](extraction.md#recover-the-existing-execution).
Retain the resume idempotency key in the handoff so a replacement child can
reconcile an uncertain call. Reacquire work after resume; old leases are invalid.
For related books, coordinate [shared glossary revisions](shared-glossaries.md)
explicitly: record each project's adopted revision and local exceptions. One
book's published refinement does not update the other books or their prose.

Each book handoff reports IDs, completed stages, validated checkpoints, source
pages reviewed, artifact delivery evidence and remaining uncertainty. The
collection coordinator reconciles every assigned book against server state and
reports per-book outcomes. Do not call the collection complete while a child is
merely dispatched, a submission is only accepted, or an export is not delivered.
