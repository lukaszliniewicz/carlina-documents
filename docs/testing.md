# Carlina testing and static analysis

Use focused checks for the boundary being changed, then the full affected lane at
a coherent checkpoint. Documentation-only changes need links, namespace and
affected source-packaging checks. They do not require frontend rebuilds, native
installation or a new document extraction. The [release plan](implementation-plan.md)
separates source, installer-preview and desktop-beta acceptance.

## Python static gates

Blocking lane:

```bash
pixi run python-quality
```

It runs:

- Ruff over first-party Python for syntax, undefined names, Pyflakes/Pylint
  errors, and selected high-signal Bugbear/Ruff correctness rules;
- strict Ruff/formatting and production typing for the new-code lane, plus
  basedpyright and its locked legacy baseline for retained source;
- Import Linter's shared-core/product boundaries; and
- the exact namespace, selected Documents closure and local Markdown-link checks.

The type-check include list is intentionally explicit while the large legacy
implementation is migrated. Expand it when a component is made clean; do not
silently remove files to hide new errors.

Deeper audit:

```bash
pixi run python-audit
```

It adds:

- Vulture at 100% confidence over first-party production/tooling Python,
  excluding vendored environments and immutable migration revisions;
- the add-on-aware dependency-ownership validator; and
- an advisory broad correctness/security Ruff statistics inventory for
  planning legacy cleanup.

The 100%-confidence Vulture and dependency findings block this command. Lower
confidence Vulture output is deliberately not gated because dynamic Flask
routes, callbacks, registries, workers, schema hooks, and migrations create
legitimate false positives. Deletion still requires direct registration/call
evidence, not one tool report.

## Frontend static gates

```bash
npm run check
npm run lint
```

`check` runs Svelte/TypeScript diagnostics across every declared workspace.
`lint` runs ESLint over authored JavaScript/TypeScript/Svelte source and
configuration. Both exclude generated `static/frontend`, add-on `static/dist`,
vendored libraries, build output, and local dependency trees.

The blocking ESLint lane keeps the recommended high-signal Svelte rules. Two
large pre-existing migration inventories—unkeyed each blocks and Svelte-native
reactive collection wrappers—are explicitly non-blocking until they can be
ratcheted workspace by workspace. Established HTML/imperative renderers have
file-scoped exceptions; new files do not inherit them.

Compiled bundles are verified by rebuilding the affected workspace and checking
its manifest/stamp; they are not linted as authored source.

## Pytest lanes

Markers are assigned centrally in `tests/conftest.py`:

- `fast` — deterministic pull-request smoke/contract lane;
- `core` — core application/service/framework behavior;
- `addon` — first-party add-on behavior;
- `contract` — public schema, manifest, frontend, packaging, or compatibility;
- `integration` — persistence, routes, migrations, workers, or services;
- `e2e` — complete install/runtime/browser flows; and
- `uses_network` — opt-in external-provider/network tests.

Common commands:

```bash
pixi run python -m pytest -m fast -q
pixi run python -m pytest -m "core and not uses_network" -q
pixi run python -m pytest -m "addon and not uses_network" -q
pixi run python -m pytest -m "not uses_network" -q
```

The full offline suite is the repository-wide merge truth. The fast lane is an
early signal, not equivalent coverage.

Release acceptance runs the complete offline suite or complementary shards with
an exact shard-union check against collection. Retain receipts privately; an
incomplete or timed-out run is not a pass.

## Frontend tests and builds

```bash
npm test
npm run build:core
npm run build:builtins
```

Use a focused workspace while iterating, for example:

```bash
npm run test --workspace @carlina/core
npm run check --workspace @carlina/core
```

After a source-derived UI change, run the repository frontend orchestrator so
the build stamp records the exact source/lock/output set. A release check should
rebuild all committed distributions and require no diff except deliberately
ignored build timestamps restored to the committed source before integrity
inspection.

## Runtime, migration, and concurrency tests

Stateful behavior should cover, as applicable:

- clean install and existing-data upgrade;
- downgrade or explicitly forward-only behavior;
- interruption before/after every durable boundary;
- idempotent retry and stale-worker fencing;
- owner/scope isolation;
- atomic marker/root switch and rollback;
- encrypted raw-state truth and key-unavailable behavior; and
- exact cleanup of temporary generations/resources.

Concurrency claims require independent sessions/processes/threads or
deterministic barriers. A sequential mock cannot prove race behavior.

## Network and live-model tests

External/provider tests never run in the default offline suite. They require an
explicit flag, configured test account/model, redacted telemetry, bounded spend,
and exact side-effect review. Use synthetic fixtures for routine behavior.

Live model evaluation should assert structure, evidence coverage, tool/result
contracts, and policy behavior rather than brittle prose. Store private raw
artifacts outside Git.

## Native release checks

Run the selected Windows/Linux worker-environment and exact installed-artifact
checks for a binary candidate or a changed platform boundary. Verify native tool
availability, source identity, profile separation, restart and a small document
workflow. Existing-host checks establish only that scope. The desktop beta also
needs cold-machine graphical setup and uninstall/reinstall with data retained.

Windows executables are unsigned. Preserve the separate release-index signature
and artifact-hash checks; certificate procurement is not a test prerequisite.
Alternative publisher payloads need complete acceptance only when selected for
distribution. Containers, Compass clients and GPU work are outside this
composition. Linux execution or mocks do not establish native Windows behavior.

## Handoff checklist

For a repository-wide change:

1. `pixi run python-quality`
2. `pixi run python-audit` and review every new finding
3. `npm run check`
4. `npm run lint`
5. `npm test`
6. `pixi run python -m pytest -m "not uses_network" -q`
7. rebuild/verify affected generated assets
8. `git diff --check`
9. exact `git status --short --branch`

Add release/platform lanes only when the change or requested acceptance boundary
requires them.
