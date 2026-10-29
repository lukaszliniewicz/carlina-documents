# Engineering policy

This policy defines the target static, dependency and acceptance gates for the
shared runtime and standalone document application. Python checks retain the
repository-wide correctness gate and basic typing lane, and add strict lint,
formatting and production typing for new files. Standard typing covers the
shared runtime and document products with a reviewed native legacy baseline.
Import-linter contracts, 100%-confidence Vulture, dependency ownership and
namespace checks remain enforced. Checked-in policy files define the current
scope, baselines and exceptions.

## Scope and exceptions

New public contracts, composition code, moved modules and newly extracted
document/translation services are the strict lane. Existing modules enter that
lane in coherent responsibility-sized increments. The remaining legacy tree
keeps the fatal gate and a visible debt inventory; moving a file must not make
its findings disappear from either inventory.

`policy/python-quality.json` freezes the existing Python paths beneath the two
package roots, scripts and tests. Newly tracked or nonignored untracked Python
files automatically enter strict lint and formatting; new production files also
enter strict typing. Delete an obsolete legacy entry when deleting/moving its
file. To promote a retained file, remove its legacy entry and add its full path
to `strict_files`. Expanding the legacy snapshot requires an explicit policy
review; do not regenerate it from the working tree. Tests keep the existing
correctness gate and receive strict lint/formatting when new, without strict
production typing. Generated and vendor exclusions do not cover migrations or
disabled add-on source.

Every suppression/baseline expansion identifies the diagnostic, exact scope,
reason, owner and removal condition. A false positive gets a narrow suppression;
valid historical debt gets a reviewed baseline. New defects are fixed. Avoid
blanket module exclusions, blanket noqa/type-ignore, and disabling a rule globally
to hide one failure. Generated/vendor/data trees are excluded; handwritten
compatibility, migrations and production adapters still receive appropriate
checks. Record what a gate does not cover.

## Python lint and formatting

Keep the existing fatal/correctness task across core, products, scripts and tests.
For the strict lane, use an explicit stable Ruff selection: E4/E7/E9, F, I, UP,
B, SIM, C4, PIE and RUF. Review individual diagnostics and add narrowly justified
exceptions for genuine framework/protocol conventions. Do not inherit all legacy
B-rule ignores into new services. Security, async, timezone, logging and complexity
inventories guide targeted correctness work; they are not automatic proof of
vulnerabilities or a reason to rewrite every long function.

Use Ruff formatting with a declared line length and matching project settings.
Run format-check in CI for the strict lane; format legacy files when bringing
their responsibility into that lane. Do not combine broad formatting with stateful
behavior changes. Disable formatter-conflicting style rules, use stable rather
than preview rule sets, and review unsafe fixes manually. Rule selection is
explicit so tool upgrades do not silently enable every new rule. This follows
the upstream guidance on [selection and fixes](https://docs.astral.sh/ruff/linter/)
and [formatter compatibility](https://docs.astral.sh/ruff/formatter/#conflicting-lint-rules).

## Python typing

- Strictly type public service ports, DTOs, packet/result schemas, identifiers,
  owner/revision/lease fences, storage policies and composition dependencies.
  Use Protocol/TypedDict/dataclasses or existing validated schemas as appropriate;
  do not wrap every internal dictionary in a new abstraction.
- Replace unbounded Any at these boundaries with validated input and typed
  results. Keep dynamic framework/provider values localized in adapters. A cast
  is not runtime validation, and an empty permissive stub is not type coverage.
- Expand basedpyright to the document/shared dependency closure incrementally.
  Keep strict new modules outside the debt baseline. Legacy code can use a
  reviewed native baseline, with rule/file counts retained and reduced as code
  is improved. CI must not run writebaseline to approve fresh errors.
- Pin checker/configuration behavior. CI locks the baseline against writes and
  rejects new diagnostics. Local raw-diagnostic comparisons identify resolved
  entries for reviewed baseline reductions; locked replay alone does not prove
  that every retained entry is still needed. Inspect diagnostic identity on moved
  code: a baseline can suppress an error with the same matching signature.
- Test target-platform import paths and compiled dependencies with the actual
  interpreter. All-platform static analysis does not prove native ABI or tool
  availability. Dependency stubs must match the locked production packages.

basedpyright has [native baseline support](https://docs.basedpyright.com/latest/benefits-over-pyright/baseline/).
Its [CLI behavior](https://docs.basedpyright.com/latest/configuration/command-line/)
normally removes resolved entries locally and locks the baseline in recognized
CI environments. Verify this behavior under the chosen pinned version rather
than building a second count-only suppression system. The present zero-error
basic/include-list result does not certify all product modules or the quality
of unannotated/dynamic interfaces.

## Dependencies and cycles

Enforce import direction around real composition seams:

| Surface | Allowed dependencies |
|---|---|
| Shared contracts and pure semantic types | Standard library and explicitly owned contract libraries |
| Shared runtime services | Shared contracts and runtime adapters; no product or unrelated add-on imports |
| Document/translation engine | Contracts and its own domain modules; no Flask startup or assistant services |
| Product host/transport adapter | Its engine plus public runtime/HTTP/MCP ports |
| Composition root | Service and product registrations; explicit optional integration selection |
| Optional Knowledge/other integration | Public product/runtime contracts; never an engine prerequisite |

Use import-linter forbidden/layer/independence or supported acyclic contracts,
with focused cycle analysis for the shared/product closure. Begin with the new
boundaries and grow coverage. Transitional wrappers preserve the identity of
model classes and registries. The enforced contracts cover canonical core,
the product-independent MCP tool runtime, and the Document Processor and
Translator registration adapters' independence from the gateway. They do not
check the entire legacy tree for cycles. Selected document products and MCP also
have a direct-import prohibition on Workbench capture API source. Shared composition
roots retain conditional imports; exact-wheel guards check exercised Documents
startup, activation and passive/recovery paths instead of claiming whole-program
transitive independence. The legacy `core` and `addons` namespace
directories are explicit analysis roots: Grimp otherwise skips them beneath the
regular package. Nested namespace coverage remains incremental. Import-linter
checks source imports; string-dispatched tools/capabilities, routes and dynamic
registration need source traces and boot tests as well.

Vulture remains blocking at 100% confidence. Whitelist genuine dynamic
registration through explicit owned evidence; do not delete migrations, entry
points or handlers on a heuristic alone. Lower-confidence inventories are
advisory. Graphs aid navigation and cannot certify import direction, dead code
or cycle absence, especially for partially extracted Svelte or unresolved edges.
Ruff recognizes the external `V107` annotation so an abstract protocol's public
parameter names can carry line-specific Vulture exceptions. The native SaT
protocol owns five such exceptions: its constructor keywords and paragraph
keyword are exercised by the engine tests. Remove them if the protocol is
replaced or Vulture learns to handle abstract signatures; they do not exempt
implementation parameters or whole modules.

## Environment and dependency ownership

Each direct dependency has a declared owner and required execution locations:
shared runtime, product host, managed worker, frontend build, quality/validation
or optional integration. Deliberate copies in isolated environments are allowed
when callers require them, with explicit version parity and import/tool-origin
evidence. Root metadata declares package requirements; committed Pixi and hashed
host locks select exact artifacts. Broad ranges are not a replacement for locks,
and a declared bound is not the installed version.

- Keep the selected Python minor consistent across in-process host bridges;
  test ABI/native library imports. Managed subprocesses may use a distinct
  environment only through a typed IPC boundary.
- One process must not rely on import precedence between incompatible vendor
  copies. Run dependency-ownership checks and import-origin probes after changes.
- Give Pandoc, PyMuPDF/PDFium, Graphviz, fonts and validation tools explicit
  owner/paths. Host and worker Graphviz copies currently serve different caller
  prefixes; verify both. Avoid competing Pandoc binaries in one production
  environment. Include font/license resources for the scripts claimed by exports.
- Keep Node, compilers, test tools, browsers and unrelated speech/OCR services
  out of the production document closure. Removing a dependency also requires
  removing or deferring its imports and startup services.
- Use `pixi run python scripts/measure_documents_closure.py --output NEW_REPORT`
  for selected backend source and owner-labelled locks. Add `--wheel WHEEL` and
  labelled `--tree LABEL=PATH` inputs for artifact/storage snapshots. Tree totals
  sum per-path stat sizes, including directory/link metadata and repeated hard
  links; allocated bytes do not prove exclusive use or reclaimed space.
  `runtime_snapshot(app)` captures the calling process after boot, including
  collector imports; only its metadata schema is accepted by `--runtime-snapshot`.
  Use fresh owned roots for runtime probes. Snapshot totals cannot establish peak
  install space; measure that separately around the actual bootstrap.
- `pixi run documents-closure-check` enforces the reviewed source-owner member/
  byte limits and Linux lock-context counts in `policy/documents-closure.json`.
  Run its script with `--platform win-64` for the Windows locks and `--wheel WHEEL`
  for an exact artifact. The release builder executes the exported revision's
  policy for both platforms before copying output. These are growth ceilings,
  not a minimal-dependency or measured-peak claim. Changes need owner rationale,
  measured evidence and affected acceptance; do not raise them to silence a failure.
  Schema 2 also classifies every selected public/legacy host file into an explicit
  retention group with a reason. New or stale members fail; globs, duplicate paths,
  malformed groups and untrusted measurement paths are rejected. Keep classification
  and source selection reviewed together; the policy accounts for shipped files,
  not arbitrary callback reachability. Development tools belong in the quality
  environment rather than the managed Documents environment.
  Fresh-process Documents boot also enforces the cold host-import ceiling before
  health requests or product activation. Workflow imports have a different scope.
- Use lock-consistency checks for CI/install. Pixi
  [locked mode](https://pixi.prefix.dev/latest/reference/cli/pixi/install/) rejects
  manifest drift; frozen mode uses a lock even when the manifest disagrees.
  Frozen replay is useful for exact artifacts but does not prove consistency.
- Run npm ci against committed frontend locks. Ship prebuilt, stamp-verified
  assets; do not require Node during ordinary installation.
- Review npm audit against the full workspace lock and the shipped source
  composition at a release checkpoint. Resolve affected runtime/build families
  with aligned peers and affected-lane evidence; record any unresolved finding
  in the release exception policy before public artifact acceptance.

Upgrade in coherent groups: quality tools; Python runtime/platform wheels;
framework/persistence; MCP SDK/protocol; LLM adapter; document converters/fonts;
frontend framework/build; editor family. Refresh upstream metadata, changelogs,
security and license changes before choosing a candidate. Test the affected
contracts and clean installed artifacts. Major versions and releases outside
current caps get a bounded compatibility experiment, not a blind latest sweep.
Do not upgrade unrelated add-ons merely because a registry lists newer versions.

Record each exception or deferred major with owner, reason and revisit trigger.
Keep Python 3.14 until the measured product/tool closure supports a justified
change; obsolete Python 3.11 install files are not a reason to downgrade.
Both Carlina and standalone app use the same accepted package/lock generations
for shared dependencies, rather than independently drifting manifests.

## Frontend and contracts

TypeScript/Svelte check and ESLint are required for handwritten source. Type the
HTTP/SDK boundary and domain state. Translator JS with checkJs disabled receives
an explicit incremental typing lane; a green Svelte check must not hide that gap.
Enable disabled correctness rules on touched responsibilities with targeted
exceptions for intentional behavior. Keep sanitization and safe HTML rendering
contracts, and coordinate TipTap-family upgrades with semantic round trips.

The parent owns component/layout/copy/interaction changes and browser acceptance.
After UI edits run affected static/tests and rebuild distributions/stamps. Check
keyboard, accessibility, narrow screens and error/recovery states in the actual
application; tests/source alone do not establish the rendered result.

## Behavioral and artifact acceptance

Use focused tests for material semantics, state or failure risks. Do not add tests
that simply mirror a reversible prose change. Preserve source identities,
footnote references/backlinks, list/table continuity, review revisions and export
witnesses through decomposition. Validate structured artifacts and inspect their
actual renderings. Job acceptance is not a usable completed output.

Stateful changes need forward migration plus rollback or explicit forward-only
handling, interruption/retry, owner isolation, root ownership and raw encrypted
record truth. Concurrent/idempotent behavior needs independent contexts or a
deterministic barrier. Synthetic data is the default; managed roots/provider
accounts require the task's explicit action scope.

Require clean artifact installation outside the source checkout, package-member
and license allowlists, import origins, native tool availability and real Windows
execution. Test installed web and worker closures. Packaging/schema/profile
changes run the full affected backend lane; repository-wide changes run the
offline suite. No-network fakes prove mechanics, not real-provider compatibility.

## Development and acceptance cadence

Commit a coherent behavior or integration milestone, not every successful probe
or documentation status update. A small recoverable Git checkpoint is inexpensive;
an artifact acceptance checkpoint requires immutable source identity and receipts.
Do not conflate the two or use fewer checkpoints to excuse incomplete stateful
verification.

During implementation run the focused lane that exercises the changed contract.
At integration run the full affected lane and required static gates. Run the broad
offline suite for repository-wide changes or a broad candidate milestone; repeat
it only for changed scope, a failure or unresolved evidence. Native matrices apply
to changed platform boundaries and the exact candidate, not every prose update.
Preserve a failed receipt; make a new bounded continuation instead of silently
rewriting its result. Reuse artifact/render evidence when exact hashes match.

Before an expensive native run, audit its command/schema/pin expectations locally.
Prefer the accepted transport, process fences and corpus over a new per-version
harness. Tests must target real contracts; an assertion stricter than the product
contract is a fixture defect. Keep machine-specific evidence and release planning
outside the public source; Git history retains superseded implementation details.

## Gate inventory

| Gate | Current command | Target addition |
|---|---|---|
| Python correctness/type/boundary/namespace/docs | pixi run python-quality, including strict new-file lint/format/typing and locked standard legacy baseline | Promote coherent legacy responsibilities and reduce the baseline |
| Dead code/dependency ownership/debt | pixi run python-audit | Explicit closure/cycle coverage; advisory debt stays visible |
| Backend behavior | pixi run python -m pytest -m "not uses_network" -q | Profile and installed-artifact acceptance alongside focused tests |
| Frontend static | npm run check; npm run lint | Type unchecked JS seams; preserve explicit exceptions |
| Frontend behavior/assets | npm test; affected builds/stamps | Focused shell browser/artifact acceptance |
| Native release | Existing packaging/lifecycle lanes | Clean native Windows/Linux document installation and failure matrix |

If an aggregate gate stops early, run unreached components separately when
needed and report them as separate evidence. A task using exit-zero remains
advisory even when its shell status is green. CI and local tasks must agree on
tool versions, scope and flags; identify an ambient launcher separately from the
pinned managed runtime. Only claim an entire lane passed when it actually ran.
