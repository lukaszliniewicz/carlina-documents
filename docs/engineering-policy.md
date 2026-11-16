# Engineering policy

This policy defines the target static, dependency and acceptance gates for the
shared runtime and the standalone document application. Python checks keep the
repository-wide correctness gate and the basic typing lane. They add strict lint,
formatting and production typing for new files. Standard typing covers the shared
runtime and document products, with a reviewed native legacy baseline.
Import-linter contracts, 100%-confidence Vulture, dependency ownership and
namespace checks remain enforced. Checked-in policy files define the current
scope, baselines and exceptions.

## Scope and exceptions

New public contracts, composition code, moved modules and newly extracted
document and translation services are the strict lane. Existing modules enter
that lane in coherent increments, each sized to one responsibility. The remaining
legacy tree keeps the fatal gate and a visible debt inventory. Moving a file must
not make its findings disappear from either inventory.

`policy/python-quality.json` freezes the existing Python paths beneath the two
package roots, scripts and tests. Newly tracked Python files, and untracked ones
that are not ignored, automatically enter strict lint and formatting. New
production files also enter strict typing. When you delete or move a file, delete
its obsolete legacy entry. To promote a retained file, remove its legacy entry
and add its full path to `strict_files`. Expanding the legacy snapshot requires an
explicit policy review, so do not regenerate it from the working tree. Tests keep
the existing correctness gate. New tests receive strict lint and formatting, but
not strict production typing. Generated and vendor exclusions do not cover
migrations or disabled add-on source.

Every suppression or baseline expansion identifies the diagnostic, exact scope,
reason, owner and removal condition. A false positive gets a narrow suppression.
Valid historical debt gets a reviewed baseline. New defects are fixed. Avoid
blanket module exclusions, blanket noqa or type-ignore, and disabling a rule
globally to hide one failure. Generated, vendor and data trees are excluded.
Handwritten compatibility code, migrations and production adapters still receive
appropriate checks. Record what a gate does not cover.

## Python lint and formatting

Keep the existing fatal correctness task across core, products, scripts and
tests. For the strict lane, use an explicit, stable Ruff selection: E4/E7/E9, F,
I, UP, B, SIM, C4, PIE and RUF. Review individual diagnostics and add narrowly
justified exceptions for genuine framework and protocol conventions. Do not
inherit all legacy B-rule ignores into new services. Security, async, timezone,
logging and complexity inventories guide targeted correctness work. They are not
automatic proof of vulnerabilities, and they are not a reason to rewrite every
long function.

Use Ruff formatting with a declared line length and matching project settings.
Run format-check in CI for the strict lane. Format legacy files when bringing
their responsibility into that lane. Do not combine broad formatting with
stateful behaviour changes. Disable formatter-conflicting style rules, use stable
rather than preview rule sets, and review unsafe fixes manually. Rule selection
is explicit, so tool upgrades do not silently enable every new rule. This follows
the upstream guidance on [selection and fixes](https://docs.astral.sh/ruff/linter/)
and [formatter compatibility](https://docs.astral.sh/ruff/formatter/#conflicting-lint-rules).

## Python typing

- Type public service ports, DTOs, packet and result schemas, identifiers, owner,
  revision and lease fences, storage policies and composition dependencies
  strictly. Use Protocol, TypedDict, dataclasses or existing validated schemas as
  appropriate. Do not wrap every internal dictionary in a new abstraction.
- Replace unbounded Any at these boundaries with validated input and typed
  results. Keep dynamic framework and provider values localised in adapters. A
  cast is not runtime validation, and an empty permissive stub is not type
  coverage.
- Expand basedpyright to the document and shared dependency closure
  incrementally. Keep strict new modules outside the debt baseline. Legacy code
  can use a reviewed native baseline, with rule and file counts kept and reduced
  as code is improved. CI must not run writebaseline to approve fresh errors.
- Pin checker and configuration behaviour. CI locks the baseline against writes
  and rejects new diagnostics. Local raw-diagnostic comparisons identify resolved
  entries for reviewed baseline reductions. A locked replay alone does not prove
  that every retained entry is still needed. Inspect diagnostic identity on moved
  code: a baseline can suppress an error with the same matching signature.
- Test target-platform import paths and compiled dependencies with the actual
  interpreter. All-platform static analysis does not prove native ABI or tool
  availability. Dependency stubs must match the locked production packages.

basedpyright has [native baseline support](https://docs.basedpyright.com/latest/benefits-over-pyright/baseline/).
Its [CLI behaviour](https://docs.basedpyright.com/latest/configuration/command-line/)
normally removes resolved entries locally and locks the baseline in recognised
CI environments. Verify this behaviour under the chosen pinned version rather
than building a second count-only suppression system. The present zero-error
basic include-list result does not certify all product modules or the quality of
unannotated or dynamic interfaces.

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

Use import-linter forbidden, layer, independence or supported acyclic contracts,
with focused cycle analysis for the shared and product closure. Begin with the
new boundaries and grow coverage. Transitional wrappers preserve the identity of
model classes and registries. The enforced contracts cover canonical core, the
product-independent MCP tool runtime, and the independence of the Document
Processor and Translator registration adapters from the gateway. They do not
check the entire legacy tree for cycles. Selected document products and MCP also
have a direct-import prohibition on Workbench capture API source. Shared
composition roots keep conditional imports. Exact-wheel guards check exercised
Documents startup, activation and passive and recovery paths, instead of claiming
whole-program transitive independence. The legacy `core` and `addons` namespace
directories are explicit analysis roots, because Grimp otherwise skips them
beneath the regular package. Nested namespace coverage remains incremental.
Import-linter checks source imports. String-dispatched tools and capabilities,
routes and dynamic registration also need source traces and boot tests.

Vulture remains blocking at 100% confidence. Whitelist genuine dynamic
registration through explicit owned evidence. Do not delete migrations, entry
points or handlers on a heuristic alone. Lower-confidence inventories are
advisory. Graphs aid navigation and cannot certify import direction, dead code or
cycle absence, especially for partially extracted Svelte or unresolved edges.
Ruff recognises the external `V107` annotation, so an abstract protocol's public
parameter names can carry line-specific Vulture exceptions. The native SaT
protocol owns five such exceptions: its constructor keywords and paragraph
keyword are exercised by the engine tests. Remove them if the protocol is
replaced or Vulture learns to handle abstract signatures. They do not exempt
implementation parameters or whole modules.

## Environment and dependency ownership

Each direct dependency has a declared owner and required execution locations:
shared runtime, product host, managed worker, frontend build, quality or
validation, or optional integration. Deliberate copies in isolated environments
are allowed when callers require them, with explicit version parity and
import-origin and tool-origin evidence. Root metadata declares package
requirements, and committed Pixi and hashed host locks select exact artifacts.
Broad ranges are not a replacement for locks, and a declared bound is not the
installed version.

- Keep the selected Python minor version consistent across in-process host
  bridges, and test ABI and native library imports. Managed subprocesses may use
  a distinct environment only through a typed IPC boundary.
- One process must not rely on import precedence between incompatible vendor
  copies. Run dependency-ownership checks and import-origin probes after changes.
- Give Pandoc, PyMuPDF and PDFium, Graphviz, fonts and validation tools an
  explicit owner and path. Host and worker Graphviz copies currently serve
  different caller prefixes, so verify both. Avoid competing Pandoc binaries in
  one production environment. Include font and licence resources for the scripts
  claimed by exports.
- Keep Node, compilers, test tools, browsers and unrelated speech and OCR
  services out of the production document closure. Removing a dependency also
  requires removing or deferring its imports and startup services.
- Use `pixi run python scripts/measure_documents_closure.py --output NEW_REPORT`
  to measure the selected backend source and owner-labelled locks. Add
  `--wheel WHEEL` and labelled `--tree LABEL=PATH` inputs for artifact and storage
  snapshots. Tree totals sum per-path stat sizes, including directory and link
  metadata and repeated hard links. Allocated bytes do not prove exclusive use or
  reclaimed space. `runtime_snapshot(app)` captures the calling process after
  boot, including collector imports. Only its metadata schema is accepted by
  `--runtime-snapshot`. Use fresh owned roots for runtime probes. Snapshot totals
  cannot establish peak install space, so measure that separately around the
  actual bootstrap.
- `pixi run documents-closure-check` enforces the reviewed source-owner member and
  byte limits and the Linux lock-context counts in `policy/documents-closure.json`.
  Run its script with `--platform win-64` for the Windows locks and `--wheel WHEEL`
  for an exact artifact. The release builder executes the exported revision's
  policy for both platforms before copying output. These are growth ceilings, not
  a minimal-dependency or measured-peak claim. Changes need owner rationale,
  measured evidence and affected acceptance. Do not raise the limits to silence a
  failure. Schema 2 also classifies every selected public and legacy host file
  into an explicit retention group with a reason. New or stale members fail.
  Globs, duplicate paths, malformed groups and untrusted measurement paths are
  rejected. Keep classification and source selection reviewed together. The
  policy accounts for shipped files, not arbitrary callback reachability.
  Development tools belong in the quality environment rather than the managed
  Documents environment. Fresh-process Documents boot also enforces the cold
  host-import ceiling before health requests or product activation. Workflow
  imports have a different scope.
- Use lock-consistency checks for CI and installation. Pixi
  [locked mode](https://pixi.prefix.dev/latest/reference/cli/pixi/install/) rejects
  manifest drift, while frozen mode uses a lock even when the manifest disagrees.
  Frozen replay is useful for exact artifacts but does not prove consistency.
- Run npm ci against committed frontend locks. Ship prebuilt, stamp-verified
  assets, and do not require Node during ordinary installation.
- Review npm audit against the full workspace lock and the shipped source
  composition at a release checkpoint. Resolve affected runtime and build
  families with aligned peers and affected-lane evidence. Record any unresolved
  finding in the release exception policy before public artifact acceptance.

Upgrade in coherent groups: quality tools; Python runtime and platform wheels;
framework and persistence; MCP SDK and protocol; LLM adapter; document converters
and fonts; frontend framework and build; editor family. Refresh upstream
metadata, changelogs, security and licence changes before choosing a candidate.
Test the affected contracts and clean installed artifacts. Major versions and
releases outside current caps get a bounded compatibility experiment, not a blind
latest sweep. Do not upgrade unrelated add-ons merely because a registry lists
newer versions.

Record each exception or deferred major with owner, reason and revisit trigger.
Keep Python 3.14 until the measured product and tool closure supports a justified
change. Obsolete Python 3.11 install files are not a reason to downgrade.
Carlina and the standalone app both use the same accepted package and lock
generations for shared dependencies, instead of independently drifting manifests.

## Frontend and contracts

The Svelte and TypeScript check and ESLint are required for handwritten source.
Type the HTTP and SDK boundary and the domain state. Translator JS with checkJs
disabled receives an explicit incremental typing lane. A green Svelte check must
not hide that gap. Enable disabled correctness rules on touched responsibilities,
with targeted exceptions for intentional behaviour. Keep sanitisation and safe
HTML rendering contracts, and coordinate TipTap-family upgrades with semantic
round trips.

The parent owns component, layout, copy and interaction changes and browser
acceptance. After UI edits, run the affected static checks and tests and rebuild
distributions and stamps. Check keyboard use, accessibility, narrow screens and
error and recovery states in the actual application. Tests and source alone do
not establish the rendered result.

## Behavioral and artifact acceptance

Use focused tests for material semantics, state or failure risks. Do not add
tests that simply mirror a reversible prose change. Preserve source identities,
footnote references and backlinks, list and table continuity, review revisions
and export witnesses through decomposition. Validate structured artifacts and
inspect their actual renderings. Job acceptance is not a usable completed output.

Stateful changes need a forward migration plus rollback or explicit forward-only
handling, interruption and retry, owner isolation, root ownership and raw
encrypted record truth. Concurrent or idempotent behaviour needs independent
contexts or a deterministic barrier. Synthetic data is the default. Managed roots
and provider accounts require the task's explicit action scope.

Require clean artifact installation outside the source checkout, package-member
and licence allowlists, import origins, native tool availability and real Windows
execution. Test installed web and worker closures. Packaging, schema and profile
changes run the full affected backend lane, and repository-wide changes run the
offline suite. No-network fakes prove mechanics, not real-provider compatibility.

## Development and acceptance cadence

Commit a coherent behaviour or integration milestone, not every successful probe
or documentation status update. A small recoverable Git checkpoint is
inexpensive. An artifact acceptance checkpoint requires immutable source identity
and receipts. Do not conflate the two, and do not use fewer checkpoints to excuse
incomplete stateful verification.

During implementation, run the focused lane that exercises the changed contract.
At integration, run the full affected lane and the required static gates. Run the
broad offline suite for repository-wide changes or a broad candidate milestone.
Repeat it only for changed scope, a failure or unresolved evidence. Native
matrices apply to changed platform boundaries and the exact candidate, not to
every prose update. Preserve a failed receipt, and make a new bounded
continuation instead of silently rewriting its result. Reuse artifact and render
evidence when exact hashes match.

Before an expensive native run, audit its command, schema and pin expectations
locally. Prefer the accepted transport, process fences and corpus over a new
per-version harness. Tests must target real contracts, and an assertion stricter
than the product contract is a fixture defect. Keep machine-specific evidence and
release planning outside the public source. Git history retains superseded
implementation details.

## Gate inventory

| Gate | Current command | Target addition |
|---|---|---|
| Python correctness, type, boundary, namespace and docs | pixi run python-quality, including strict new-file lint/format/typing and locked standard legacy baseline | Promote coherent legacy responsibilities and reduce the baseline |
| Dead code, dependency ownership and debt | pixi run python-audit | Explicit closure and cycle coverage; advisory debt stays visible |
| Backend behaviour | pixi run python -m pytest -m "not uses_network" -q | Profile and installed-artifact acceptance alongside focused tests |
| Frontend static | npm run check; npm run lint | Type unchecked JS seams; preserve explicit exceptions |
| Frontend behaviour and assets | npm test; affected builds/stamps | Focused shell browser/artifact acceptance |
| Native release | Existing packaging/lifecycle lanes | Clean native Windows and Linux document installation and failure matrix |

If an aggregate gate stops early, run unreached components separately when
needed and report them as separate evidence. A task using exit-zero remains
advisory even when its shell status is green. CI and local tasks must agree on
tool versions, scope and flags. Identify an ambient launcher separately from the
pinned managed runtime. Only claim an entire lane passed when it actually ran.
