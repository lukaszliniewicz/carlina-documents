# Carlina Add-on API v1

Add-on API v1 is the stable contract between Carlina's core and extension
packages. It supports tools, workspaces and UI contributions, providers,
workers, scheduled jobs, workflows, skills, and add-on-owned storage without
requiring an add-on to import private manager internals.

## Trust model

Carlina does not assign one numeric tier to a whole package. Security review is
the combination of authenticated provenance, immutable archive and contract
hashes, declared permissions/resources, operator approvals, and every runtime
unit's actual placement.

The host derives `effective_code_access` from the package's most privileged
runtime unit:

- `host_process`: an `in_process` unit is imported into Flask and has Carlina's
  filesystem, environment, network, database, and application privileges;
- `host_user_process`: a supervised `managed_process` runs as the host user;
- `container_service`: code executes behind the configured container runtime;
- `remote_service`: the implementation is reached over a network contract;
- `declarative_only`: the package declares no executable runtime unit.

This is a description, not a risk score. Managed processes and containers are
process boundaries, not blanket sandbox guarantees. A mixed package is shown
at its highest access level; for example, a container with an in-process
adapter is `host_process` and requires explicit native-code approval.

The first-party catalog authenticates publisher and channel with an Ed25519
signature and binds approval to the exact catalog, archive, manifest, and
resolved-contract digests. A local ZIP still receives bounded archive and
no-import contract validation, but has no authenticated catalog publisher.
Package distribution (`built_in`, `first_party`, `community`, `private`, or
`local_development`) never implies code isolation or permission.

## Package contract

An installable ZIP contains exactly one package tree and one `manifest.json`.
Manifest schema 2 is canonical for new and migrated first-party packages. It
declares:

- `package_format_version: 1`, `manifest_schema_version: 2`, and
  `addon_api.requires` (currently `>=1,<2`);
- stable package identity and exactly one product-shape `kind`:
  `application`, `toolkit`, `connector`, `engine`, or `component`;
- a `distribution` channel, publisher, and optional source URI, independently
  of product shape and runtime placement;
- `runtime.units`, including an empty list for a purely declarative package;
- explicit `ui.entries` using `none`, `schema`, `svelte_application`,
  `svelte_component`, or `external` delivery rather than a frontend tier;
- versioned `provides.capabilities` and `requires.capabilities`, plus
  `requires.packages` only for hard code/data coupling;
- explicit `permissions`, `resources`, `storage`, dependencies, migrations,
  and JSON Schema inputs/outputs, including intentional empty collections.

One package can combine runtime units and UI contributions without changing
its primary kind. Runtime placement uses the names `in_process`,
`managed_process`, `container`, and `remote`. Each Python dependency
environment binds explicitly to one or more compatible runtime-unit IDs and
chooses `in_process_vendor` or `managed_process_pixi`. Pixi is the sole
managed-process environment manager.

Schema-1 manifests remain readable through a deterministic compatibility
projection during the deprecation window. The old package execution value,
frontend tier, and `tier1`/`tier2`/`tier2_pixi` dependency names are input
aliases for old packages, not fields emitted by new scaffolds or UI.

Installed packages compile in strict mode. Compilation validates the JSON
Schema and cross-references, performs semantic checks, freezes nested values,
and produces deterministic manifest and contract SHA-256 values. It never
imports package code.

Use the starter at `examples/addons/starter_addon`, or inspect the three focused
reference packages alongside it. Validate a directory, manifest, or ZIP with:

```console
carlina addon validate examples/addons/starter_addon
carlina addon validate --json path/to/addon.zip
```

An external add-on test suite can use:

```python
from carlina.addons.api.testing import assert_valid_package

contract = assert_valid_package(".")
assert contract.api_requires == ">=1,<2"
```

## Dependencies and imports

Installed packages may not run arbitrary post-install commands. Python
dependencies require a UTF-8 lock file in which every requirement uses an
exact `name==version` pin and at least one `--hash=sha256:...`. Includes,
indexes, editable installs, options, and unhashed records are rejected. Carlina
installs with `--require-hashes` into a hash-addressed directory in the
platform-local Carlina cache and reuses a completed environment after
interruption. The cache is namespaced by DataRoot identity, excluded from
backups, and safe to rebuild. This keeps native files and links off SMB/UNC
storage even when persistent Carlina data lives there. Operators may select a
different local volume with `CARLINA_ADDON_ENVIRONMENTS_DIR`.

Dependency directories are registered with a last-chance import finder. They
cannot shadow the standard library, Carlina, or packages already present in
the application environment. Two in-process add-ons may co-provide a top-level
dependency only when its import artifact is byte-identical after generated
bytecode is excluded. Modules already supplied by the application environment
do not participate because the last-chance finder can never select the add-on
copy. For an actual fallback dependency, the first registered copy is used and
another identical copy takes over if it is unregistered. Different artifacts
fail with a deterministic conflict instead of depending on `sys.path` order;
align the locked versions or move one runtime unit to an isolated managed
process.

`in_process_vendor` is dependency provisioning, not Python isolation. Ordinary
application packages always win, so an add-on that needs a version incompatible
with Carlina's host environment must use `managed_process_pixi` (or another
process/container boundary). Vendor cache markers bind native wheels to the
host implementation, Python version/ABI, and platform, and `uv` is invoked with
that exact interpreter. A Python upgrade therefore rebuilds the cache instead
of reusing incompatible native extensions.

New schema-2 manifests use `dependencies.python.environments`. Every entry has
a stable `id`, an exact `python_version` in `major.minor` form, and explicit
`runtime_units`. An `in_process_vendor` entry declares a hash-enforced pip lock;
a `managed_process_pixi` entry declares both its reviewed Pixi manifest and
lock. A mixed package therefore declares its Flask imports and each managed
worker independently instead of relying on one package-wide environment.

```json
{
  "dependencies": {
    "python": {
      "environments": [
        {
          "id": "host",
          "environment": "in_process_vendor",
          "runtime_units": ["host"],
          "python_version": "3.14",
          "lockfile": "requirements-host.lock",
          "lock_format": "pip_requirements",
          "require_hashes": true,
          "pip_packages": ["example>=1,<2"]
        },
        {
          "id": "worker",
          "environment": "managed_process_pixi",
          "runtime_units": ["worker"],
          "python_version": "3.11",
          "manifest": "pixi.toml",
          "lockfile": "pixi.lock",
          "lock_format": "pixi",
          "require_hashes": false
        }
      ]
    }
  }
}
```

A package may declare only one in-process environment because every in-process
unit shares Flask's import namespace. It may declare multiple managed
environments. Duplicate environment IDs, duplicate runtime bindings, mode
mismatches, unbound Pixi runtime units, missing artifacts, and duplicate setup
actions fail compilation. Lock and Pixi-manifest digests participate in the
resolved environment identity, so a dependency change creates a new immutable
cache generation.

Container dependency inputs belong to the container runtime unit rather than a
host Python environment. Declare every immutable build lock in
`runtime.units[].dependency_artifacts`; Carlina validates the file without
executing a solver, records its SHA-256 digest in the compiled contract, and
rejects duplicate ownership across runtime units. `pip_requirements` artifacts
must contain exact pins with hashes. `conda_explicit` artifacts must be UTF-8,
contain one `@EXPLICIT` marker, and use credential-free HTTPS package URLs with
integrity fragments. Neither format is installed into Flask.

```json
{
  "id": "service",
  "mode": "container",
  "dependency_artifacts": [
    {
      "path": "container/requirements.lock",
      "format": "pip_requirements"
    },
    {
      "path": "container/conda-linux-64.lock",
      "format": "conda_explicit"
    }
  ]
}
```

The historical package-level object and `tier1`, `tier2`, `tier2_pixi`, and
`pixi` spellings remain schema-1 migration inputs only. Existing package-local
`lib` directories are temporary read-only compatibility sources; Carlina no
longer creates dependency state inside the program tree. Micromamba declarations
are rejected with migration guidance.

Do not assume a worker-only package is importable by Flask routes. If an
optional dependency has terms that require an operator decision, declare a
top-level `dependency_notices` record with stable `id`, `packages`, `license`,
`purpose`, and `source_url`. The Add-on Manager shows the notice, requires a
typed confirmation, and the activation API rejects installation until every
`review_required` notice ID is acknowledged. Uploaded packages use a safe
two-pass flow: Carlina stages and validates the first upload, returns the
declared notices without publishing code, then retains activation authority
only after the administrator reviews the notices and resubmits the same package.
The acknowledged stable IDs are recorded in the activation lifecycle event.
This records informed activation; it does not determine license compatibility
for the operator.

## Owned paths

`AddonContext` exposes paths with explicit retention semantics:

- `code_dir`: selected, immutable package code;
- `data_dir`: persistent private add-on data, included in backups;
- `environment_dir` and `cache_dir`: rebuildable state;
- `logs_dir`: rotating logs;
- `uploads_dir` and `artifacts_dir`: user-visible or generated content;
- `state_dir`: persistent operational state;
- `temp_dir` and `locks_dir`: ephemeral state.

Use these paths instead of deriving locations from `__file__`, Flask's
instance path, or the current working directory. Vector-store namespaces use
a digest-backed storage key so punctuation-normalized add-on IDs cannot
collide.

## Lifecycle and retention

The persisted lifecycle journal separates these operator actions:

1. Install/upgrade stages, verifies, validates, prepares dependencies and
   assets, migrates declared schema, imports code, runs the hook, then exposes
   routes and capabilities.
2. Disable closes dispatch first, stops workers/jobs/services, calls the
   non-destructive hook, and retains code and data.
3. Remove code is available only to installed packages and preserves owned
   files, schema, contract snapshot, and lifecycle history.
4. Purge data requires the exact `PURGE <addon-id>` confirmation, creates a
   safety backup, runs the explicit purge hook and migration downgrade, then
   removes scoped owned data.

Interrupted transitions are journaled and resumable. Failed first installs
remove the unpublished version; failed upgrades restore the prior selected
version. Inactive add-ons are not imported on ordinary startup, and registered
Flask routes remain behind a live activation gate.

Built-in package versions are reconciled only inside the explicit application
`init/update` maintenance transaction, after core migrations and (for managed
updates) after a verified paired backup. Active built-ins run their declared
Alembic migrations and `on_upgrade()` hook in dependency order before their
stored version advances. Inactive built-ins retain their prior version and are
reconciled on their next activation. If ordinary startup sees an active
built-in version mismatch, it leaves the persisted active state and data
untouched but does not import the new package; run `carlina init --repair`
while the managed runtime is stopped.

This describes the current transitional path. ADR 0010 moves required/default
packages to independently selected immutable generations and separates objective
impact from operator-selected recovery coverage. Until that ownership transfer is
implemented for a package, the guarded built-in reconciliation above remains
authoritative.

Hooks have deliberately narrow meanings:

- `on_activate()`: migrations are already complete; initialize runtime state;
- `on_upgrade(from_version)`: run version-specific, idempotent application
  work after declared migrations;
- `on_deactivate()`: stop add-on-owned runtime work; never delete user data;
- `on_purge_data()`: optional destructive cleanup, called only by confirmed
  purge;
- `on_register_jobs(scheduler)`: register jobs only after activation.

## Capabilities, authorization, and UI

Consumers request an abstract, versioned capability rather than a concrete
provider package. A requirement declares operations, qualifiers, cardinality,
activation policy, and installation/user/workspace/workflow scope. A provider
declares its implementation ID, version, operations, qualifiers, runtime unit,
and optional connector definition. Resolution is deterministic; when several
compatible providers remain, the user must choose instead of inheriting import
or discovery order.

A configured external account or service is a `Connection`, not another
installed add-on. Its non-secret configuration is stored separately from
encrypted `ConnectionSecret` envelopes. The Secret Broker releases decrypted
values only to the owning connector under a scoped grant. A capability binding
selects both the provider and, where applicable, the Connection for its scope.
Add-on Doctor reports missing, ambiguous, inactive, and unhealthy bindings.

Expose callable bindings through `get_function_mapping`, but do not call raw
bindings directly. All public invocations enter Carlina's execution kernel,
which resolves the registry record, validates JSON arguments and view
restrictions, checks lifecycle availability, applies runtime policy and
approval rules, emits audit events, and only then dispatches the private
binding. The same invariant applies to chat, runs, and workflows.

UI add-ons declare stable entry/component IDs, a delivery mode, frontend API
range, assets, and input/output schemas. Runtime builds are not performed at
application startup; distributable Svelte packages must include their built
assets. Add-on routes may use `AddonContext.login_required`, but sensitive
mutations must also follow Carlina's CSRF, authorization, request-size, and
error-mapping policies.

## Migrations and compatibility

Ship reviewed Alembic migrations with any models. Carlina refuses an add-on
that declares models but omits migrations and never autogenerates revisions at
runtime. Migrations and lifecycle hooks must be idempotent because a journaled
operation can resume after process interruption.

Built-in legacy manifests run through a deterministic compatibility adapter
and receive warnings for inferred fields. Installed packages receive errors
for the same omissions. Add-on API v1 public imports live under
`carlina.addons.api`; the legacy `iarf_assistant.addon_api` path forwards to
the same objects during the compatibility window, while private
`iarf_assistant.core` modules may change without add-on compatibility
guarantees.
