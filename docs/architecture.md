# Documents architecture

Documents and private Carlina use one implementation of the host and document
engines. This repository owns that shared source. Private Carlina owns its
Workbench composition, private add-ons, deployment configuration and data.

## Source and application boundaries

The shared source keeps the `carlina` public API and the `iarf_assistant`
compatibility namespace. It also keeps the core ORM and migration lineage, the
add-on API, the model and credential services, the lifecycle and recovery
machinery, and the browser shell. The selected products are `doc_processor`,
`translator` and `sentence_segmenter`. Keeping existing imports and migration
IDs avoids a data migration for the repository separation. The release selector
keeps the compatible core ORM records and the common native host. The remaining
public and legacy host files each have an explicit reason in
[the closure policy](../policy/documents-closure.json). Additions and stale
classifications fail the quality gate. Imported, shipped and running closures
have separate acceptance gates.

Only the Workbench composition configures Compass capture authentication.
Documents does not load its capture service, principal resolver or token limiter.
The Documents artifact selector excludes `iarf_assistant/core/client_api` and its
contracts from the wheel and the corresponding source bundle. Those files stay in
this shared Git source for Workbench, whose composer does not use the Documents
artifact selector. The compatible ORM and migration records remain shared and
shipped.

Workbench-only CLI commands follow the same resolved composition as its services.
Documents keeps the account, migration, add-on and MCP administration commands.
The RAG rebuild and legacy news import commands are registered only for
Workbench. Translator uses a small shared vector backend that loads NumPy and
sqlite-vec lazily, so it does not depend on constructing the Workbench RAG
service. Canonical document Knowledge publication still uses the shared data
layer, models and migration history.

The Documents selector also omits the following from its wheel and embedded
source:

- Workbench chat, run and core-workflow executors and routes;
- code sessions and project-process supervision;
- assistant RAG and memory pruning;
- the Knowledge ingestion worker, parser and routes;
- news MCP tools; and
- audio spokes.

These implementations remain in shared Git for Workbench. Shared settings read
memory defaults from a dependency-free module, and chat tools keep their
compatible exports. Static direct-import contracts and exercised artifact import
guards protect this boundary. They do not show that arbitrary callbacks are
independent.

The agent loop, its events and the execution kernel remain because Translator
research and registered native capabilities use them. Durable runtime state and
the run and collection projections remain model dependencies. Workflow templates
and workspace-root metadata remain manager contracts. Canonical Knowledge
publication, the model and provider services, the add-on API and generation
lifecycle, and recovery remain common. Historical ORM records and migrations stay
byte-compatible, including the records for omitted Workbench services. The
classified host is deliberately compatible. It does not claim that every retained
helper runs during ordinary extraction.

At startup, Carlina selects one concrete add-on manager built on
`AddonManagerCore`. Documents uses `NativeAddonManager`, which never imports the
container backend or builds its locks or probe cache. Workbench keeps the legacy
`AddonManager` class and the container orchestration. The installation-scoped
marker naming and the manifest lifecycle policy have one shared implementation:
native activation still rejects required containers, and cleanup still removes
only owned access markers. Common discovery, queues, workers, migration and
registry behaviour stays in the shared core. The legacy facade and container
backend remain in shared Git for Workbench but are excluded from the Documents
wheel and the corresponding source. The native support mixin stays shared, and
its naming and manifest policy do not require Docker. Quality tools live in the
development environment, separate from the locked Documents and native-worker
runtime dependencies.

Documents selects three products and disables the assistant and container
services. Private Workbench assembles this source together with its own add-ons.
A pinned source dependency and a hash-inventoried build composition keep one
source owner while retaining the existing native release machinery. Generated
compositions are build outputs and never editable forks. Shared code changes
here. Private releases promote reviewed revisions instead of automatically
following main.

The reusable source tree has no Documents-only distribution descriptor. The
Documents release builder writes a closed descriptor and compiles focused routes
in an isolated stage. The resulting application wheel is restricted to Documents
and must not replace a Workbench installation. Each application owns its
assembled wheel, prebuilt assets, program generations and data root. Wheels for
the runtime and products that each have their own owner, and the transfer of
installed add-on generations, remain later work.

Release-channel defaults follow the effective application composition. Documents
selects its own repository, and Workbench keeps the shared product identity's
repository. Explicit manifest and configuration sources keep their precedence.
Signed admission still rejects a release for the other profile before any program
writes. Selecting a repository does not establish a public release channel.

## Desktop delivery

The native online entry points are shell and PowerShell scripts. The Linux
AppImage and the Windows setup EXE add a shared Python and Tk setup and launch
window over the same signed-index installation path. The window can create the
first administrator through a private stdin handoff and reopen the existing
runtime. The Windows EXE packages this adapter with PyInstaller and delegates
installation to the generated PowerShell script. Both script installers remain
available for terminal use.

New Documents roots record first-run preparation before account creation. Once an
administrator exists, initialisation activates the three bundled products through
the native lifecycle, under maintenance authority and the runtime leader lock.
The known dependency notices are attributed to the bundled distribution, and
changed required notices fail closed. A pending record permits a retry without
another account. A completed record preserves later add-on choices. Older roots
without the record, and Workbench initialisation, keep their existing behaviour.

These entry points do not provide menu integration or a managed uninstaller.
Pixi and other tool management stays internal. Data remains outside the immutable
program generations and does not depend on where the downloaded launcher lives.

Platform wrappers use the same release identity and the same installation and
lifecycle contracts. They show progress and errors and open the shared browser
application. The engines, provider setup, jobs and document UI keep one
implementation. The design calls for a small native adapter over the existing
lifecycle. The adapters use bundled portable Python and Tk for the small setup
window, with a private stdin handoff for the first administrator's credentials.
Their actual library and FUSE prerequisites still need testing. AppImage does not
imply offline setup or universal Linux compatibility. Publisher-download payloads
reconstruct the runtime from hash-pinned package downloads. Standard managed
bundles are also supported.

The Python install, update and rollback lifecycle exposes opt-in versioned events
and cooperative cancellation. Ordinary observer errors do not interrupt
initialisation or recovery. Terminal outcomes follow cleanup and ownership
release. Journals and recovery fences, not the event transport, remain the
durable authority.

Generated native entrypoints emit the same opt-in stream before Python is
available, then continue the operation ID and sequence through managed adoption.
An exclusive terminal handoff is validated and published after outer cleanup.
Native publisher payloads report their dependency, verification and environment
substeps through that same outer stream. A shared native transport generator
handles carrier and publisher download cancellation, while the caller owns
admission, progress and terminal policy. Native unpacking still finishes its
current step. The installed-Python updater consumes the same publisher records
under its existing lifecycle identity. It keeps both maintenance leases until the
child is reaped, including on cancellation and observer interruption.

Explicit retained-adoption retry binds the original transaction, the
authenticated inputs and the program and data roots before reconciliation.
Pointer and receipt evidence distinguish a committed activation from a failed or
unknown outcome before error recovery. Native entrypoints reserve their program
lease before unpacking. They bind each claimed generation to its authenticated
release and canonical roots before handing it to the manager. Failed cleanup and
quarantine preserve generations whose ownership is uncertain or foreign. Reruns
can reuse a matching ready generation, and the manager selects and validates any
retained adoption transaction under both leases.

Opt-in preparation for standard bundles and Documents publisher payloads uses a
separate custodian that retains the lease, and native process-set custody: a
Linux user scope and cgroup, or a Windows job assigned at process creation. A
completion record bound to the claim, identity and roots records native
extraction only. For publisher payloads, the custodian also validates the
complete private result protocol before publishing that record. Its file-backed
progress observer feeds the existing outer event owner without owning custody.
Resume still checks the profile and runtime, persists the first root-binding
intent, and then hands off to normal managed adoption.

A matching nonzero completion can retire an unadopted failed preparation intact
under the original program lease, after fresh release verification. A new claim
then starts extraction again. If current or previous pointers, bindings, receipts
or manager-owned state exist, the generation is preserved for managed recovery or
operator inspection. Unknown custody and old preparing claims preserve the
generation and scratch. A parent exit, a free lease or a missing job name cannot
prove completion. Unattributed scratch is not reclaimed.

Windows compiles the trusted helper with its Framework compiler through an owned
UTF-8 response file. The controller and the custodian load the same assembly
bytes. Compilation and its private diagnostics stay inside the preparation
scratch. Outer event-mode downloads can stop during a transfer, and scratch
cleanup follows curl reaping or the closure of the PowerShell-owned output
stream. This does not terminate unpacking or activation. The Linux graphical
adapter consumes this interface. See the
[update contract](update-and-recovery.md#installation-event-interface).

Both compositions reuse profile-driven onboarding components. Native model
preparation, connection setup and readiness should become one resumable user
flow. Updates retain data, root and key identity. Uninstalling should remove only
owned program and integration objects and preserve user data by default. These
are targets, not claims about the current script installers.

## State and execution

One managed runtime owns each data root. Program trees are immutable. Databases,
uploads, jobs, queues, exports, secrets and recovery records live in owned data
paths. Documents and Workbench have separate root profiles, ports and cookies.
Ordinary content in a fresh Documents root is plaintext, while credentials are
protected. Existing encrypted roots keep their recorded policy and key lineage.

Direct and passive workflows use the same semantic document and translation
contracts. Direct work requires configured providers. Passive MCP claims hydrated
packets and supplies model completions without host provider credentials. Owner,
source revision, lease, schema and idempotency checks govern acceptance. Final
publication and export checks are distinct from accepted packet submissions.

Extraction input and assembled document HTML use different note vocabularies.
New passive PDF runs freeze the `footnotes-v1` input policy. Page preflight and
worker validation enforce its marker and body shapes before assembly. Older
passive packets keep their original contract. Compact work delivery preserves the
small versioned extraction contract while referencing the omitted instructions. A
worker must retrieve and read missing instructions after a loss of context.
Hashes establish identity. They do not show that the executing model still has
the contents.

The [EPUB projection](epub-export.md) is shared by direct extraction and
translated semantic export. A named policy separates bounded edition choices from
canonical source metadata, with owner- and revision-bound preflight and immutable
delivery receipts. Archive validation is owned by the add-on and does not depend
on Flask routes. Formal accessibility assertions require an edition evaluation,
not a renderer heuristic. Historical requests that lack the field keep their
policy.

Each product owns its saved edition recipes in separate, owner-scoped ORM rows
under the root content policy. Version and source witnesses govern updates, and
tombstones preserve the concurrency history. These recipes never rewrite
canonical documents, translation plans or historical delivery receipts. A source
change retires content decisions but keeps general export preferences.

Common semantic image descriptions are bounded, inert and translatable content.
`carlina.core.semantic_images` validates them during ingestion, target
reconstruction and semantic export. The diagram projection keeps authored
descriptions and derives complete structured topology alternatives from the
validated graph. Source captions and frozen passive prompt policies remain
separate from generated accessibility adaptations and evaluated edition claims.

See [extraction](extraction-efficiency.md), [Translator](translator.md),
[MCP](mcp-integration-architecture.md), [add-on API](addon-api-v1.md),
[content policy](adr/0011-root-bound-ordinary-content-policy.md) and
[distribution decision](adr/0012-documents-agpl-distribution.md).
