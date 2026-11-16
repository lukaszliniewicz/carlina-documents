# Carlina testing and static analysis

Start with focused checks for the boundary you are changing, then run the full
affected lane at a coherent checkpoint. Documentation-only changes need the link,
namespace and affected source-packaging checks. They do not need frontend
rebuilds, native installation or a new document extraction. Validate an installer
on its advertised platform separately from source tests, because source checks
alone do not prove an installed workflow.

## Python static gates

Blocking lane:

```bash
pixi run python-quality
```

It runs:

- Ruff over first-party Python for syntax, undefined names, Pyflakes and Pylint
  errors, and selected high-signal Bugbear and Ruff correctness rules;
- strict Ruff and formatting checks and production typing for the new-code lane,
  plus basedpyright and its locked legacy baseline for retained source;
- Import Linter's shared-core and product boundaries; and
- the exact namespace, selected Documents closure and local Markdown-link checks.

The type-check include list is explicit on purpose while the large legacy
implementation is migrated. Expand it when a component is made clean. Do not
silently remove files to hide new errors.

Deeper audit:

```bash
pixi run python-audit
```

It adds:

- Vulture at 100% confidence over first-party production and tooling Python,
  excluding vendored environments and immutable migration revisions;
- the add-on-aware dependency-ownership validator; and
- an advisory Ruff statistics inventory of broad correctness and security rules,
  used to plan legacy cleanup.

The 100%-confidence Vulture findings and the dependency findings block this
command. Lower-confidence Vulture output is deliberately not gated, because
dynamic Flask routes, callbacks, registries, workers, schema hooks and migrations
produce legitimate false positives. Before deleting code, find direct
registration or call evidence. One tool report is not enough.

## Frontend static gates

```bash
npm run check
npm run lint
```

`check` runs Svelte and TypeScript diagnostics across every declared workspace.
`lint` runs ESLint over authored JavaScript, TypeScript and Svelte source and
configuration. Both exclude generated `static/frontend`, add-on `static/dist`,
vendored libraries, build output and local dependency trees.

The blocking ESLint lane keeps the recommended high-signal Svelte rules. Two
large inventories of existing migration issues, unkeyed `{#each}` blocks and
Svelte-native reactive collection wrappers, are explicitly non-blocking until
they can be ratcheted workspace by workspace. Established HTML and imperative
renderers have file-scoped exceptions, which new files do not inherit.

Compiled bundles are not linted as authored source. To verify them, rebuild the
affected workspace and check its manifest and stamp.

## Pytest lanes

Markers are assigned centrally in `tests/conftest.py`:

- `fast`: deterministic pull-request smoke and contract lane;
- `core`: core application, service and framework behaviour;
- `addon`: first-party add-on behaviour;
- `contract`: public schema, manifest, frontend, packaging or compatibility;
- `integration`: persistence, routes, migrations, workers or services;
- `e2e`: complete install, runtime and browser flows; and
- `uses_network`: opt-in tests that call external providers or the network.

Common commands:

```bash
pixi run python -m pytest -m fast -q
pixi run python -m pytest -m "core and not uses_network" -q
pixi run python -m pytest -m "addon and not uses_network" -q
pixi run python -m pytest -m "not uses_network" -q
```

The full offline suite decides whether a change can merge. The fast lane gives an
early signal but does not cover the same ground.

Release acceptance runs the complete offline suite, or complementary shards
whose union is checked exactly against test collection. Keep the receipts in a
private location. An incomplete or timed-out run is not a pass.

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

After a UI change that derives from source, run the repository frontend
orchestrator so the build stamp records the exact set of source, lock and output
files. A release check should rebuild all committed distributions and require no
diff. The exception is build timestamps, which are deliberately ignored: restore
them to the committed source before the integrity inspection.

## Runtime, migration, and concurrency tests

Tests for stateful behaviour should cover the following, as applicable:

- clean install and upgrade of existing data;
- downgrade, or explicitly forward-only behaviour;
- interruption before and after every durable boundary;
- idempotent retry and stale-worker fencing;
- owner and scope isolation;
- atomic marker and root switch, and rollback;
- encrypted raw-state truth and behaviour when the key is unavailable; and
- exact cleanup of temporary generations and resources.

Concurrency claims require independent sessions, processes or threads, or
deterministic barriers. A sequential mock cannot prove race behaviour.

## Network and live-model tests

Tests that call external providers never run in the default offline suite. They
require an explicit flag, a configured test account and model, redacted
telemetry, a bounded spend and a review of every side effect. Use synthetic
fixtures for routine behaviour.

Live model evaluation should assert structure, evidence coverage, tool and result
contracts, and policy behaviour, not brittle prose. Store private raw artefacts
outside Git.

## Native release checks

For a binary candidate or a changed platform boundary, run the selected Windows
and Linux worker-environment checks and the exact installed-artefact checks.
Verify native tool availability, source identity, profile separation, restart and
a small document workflow. Checks on an existing host establish only that scope.
The desktop beta also needs graphical setup on a cold machine, and an uninstall
and reinstall with data retained.

Windows executables are unsigned. Keep the separate release-index signature and
artefact-hash checks. Obtaining a certificate is not a prerequisite for testing.
Alternative publisher payloads need complete acceptance only when they are
selected for distribution. Containers, Compass clients and GPU work are outside
this composition. Running on Linux, or with mocks, does not establish native
Windows behaviour.

## Handoff checklist

Before handing off a repository-wide change, work through this list:

1. `pixi run python-quality`
2. `pixi run python-audit` and review every new finding
3. `npm run check`
4. `npm run lint`
5. `npm test`
6. `pixi run python -m pytest -m "not uses_network" -q`
7. rebuild and verify affected generated assets
8. `git diff --check`
9. exact `git status --short --branch`

Add release and platform lanes only when the change or the requested acceptance
boundary requires them.
