# Documents architecture

Documents and private Carlina use one implementation of the host and document
engines. This repository owns that shared source. Private Carlina owns its
Workbench composition, private add-ons, deployment configuration and data.

## Source and application boundaries

The shared source retains the `carlina` public API and `iarf_assistant`
compatibility namespace, core ORM/migration lineage, add-on API, model and
credential services, lifecycle/recovery machinery and browser shell. The selected
products are `doc_processor`, `translator` and `sentence_segmenter`.
Keeping existing imports and migration IDs avoids a data migration for repository
separation. The release selector retains compatible core ORM records and the
common native host. Its remaining public/legacy host files have explicit reasons
in [the closure policy](../policy/documents-closure.json); additions and stale
classifications fail the quality gate. Imported, shipped and running closures
have separate acceptance gates.
Compass capture authentication is configured only by the Workbench composition;
Documents does not load its capture service, principal resolver or token limiter.
Its artifact selector excludes `iarf_assistant/core/client_api` and its contracts
from the wheel and corresponding source bundle. Those files remain in this shared
Git source for Workbench; its composer does not use the Documents artifact selector.
The compatible ORM and migration records remain shared and shipped.

Workbench-only CLI commands follow the same resolved composition as its services.
Documents retains account, migration, add-on and MCP administration commands;
RAG rebuild and legacy news import are registered only for Workbench. Translator
uses a small shared vector backend for lazy NumPy/sqlite-vec loading, independent
of constructing the Workbench RAG service. Canonical document Knowledge publication
still uses the shared data layer, models and migration history.
The Documents selector also omits Workbench chat/run/core-workflow executors and
routes, code sessions, project-process supervision, assistant RAG/memory pruning,
Knowledge ingestion worker/parser/routes, news MCP tools and audio spokes from
its wheel and embedded source. These implementations remain in shared Git for
Workbench. Shared settings reads memory defaults from a dependency-free module;
chat tools retain their compatible exports. Static direct-import contracts and
exercised artifact import guards protect this boundary; they do not establish
independence of arbitrary callbacks.

The agent loop/events and execution kernel remain because Translator research
and registered native capabilities use them. Durable runtime state and run/
collection projections remain model dependencies; workflow templates and
workspace-root metadata remain manager contracts. Canonical Knowledge publication,
model/provider services, add-on API/generation lifecycle and recovery remain common.
Historical ORM records and migrations stay byte-compatible, including records
for omitted Workbench services. The classified host is deliberately compatible,
not a claim that every retained helper runs during ordinary extraction.

Startup selects one concrete add-on manager over `AddonManagerCore`. Documents
uses `NativeAddonManager`, which never imports the container backend or builds
its locks/probe cache. Workbench retains the legacy `AddonManager` class and
container orchestration. Installation-scoped marker naming and manifest lifecycle
policy have one shared implementation: native activation still rejects required
containers and cleanup still removes only owned access markers. Common discovery,
queues, workers, migration and registry behavior stays in the shared core.
The legacy facade and container backend remain in shared Git for Workbench but
are excluded from the Documents wheel and corresponding source. The native
support mixin stays shared; its naming and manifest policy do not require Docker.
Quality tools live in the development environment, separately from locked
Documents/native-worker runtime dependencies.

Documents selects three products and disables assistant/container services.
Private Workbench assembles this source plus its own add-ons. A pinned source
dependency and hash-inventoried build composition preserve one source owner while
retaining existing native release machinery. Generated compositions are build
outputs, never editable forks. Shared code changes here; private releases promote
reviewed revisions rather than automatically following main.

The reusable source tree has no Documents-only distribution descriptor. The
Documents release builder writes a closed descriptor and compiles focused routes
in an isolated stage. Its resulting application wheel is restricted to Documents;
it must not replace a Workbench installation. Each application owns its assembled
wheel, prebuilt assets, program generations and data root. Independently owned
runtime/product wheels and installed add-on generation transfer remain later work.

Release-channel defaults follow the effective application composition. Documents
selects its own repository; Workbench retains the shared product identity's
repository. Explicit manifest/configuration sources keep their precedence, and
signed admission still rejects a release for the other profile before program
writes. Repository selection does not establish a public release channel.

## Desktop delivery

The native online entry points are shell and PowerShell scripts. The Linux
AppImage and Windows setup EXE add a shared Python/Tk setup and launch window over
the same signed-index installation path. The window can create the first administrator through a private stdin
handoff and reopen the existing runtime. The Windows EXE packages this adapter
with PyInstaller and delegates installation to the generated PowerShell script.
Both script installers remain available for terminal use.
New Documents roots record first-run preparation before account creation. After an
administrator exists, initialization activates the three bundled products through
the native lifecycle under maintenance authority and the runtime leader lock.
The known dependency notices are attributed to the bundled distribution; changed
required notices fail closed. A pending record permits retry without another
account; a completed record preserves later add-on choices. Older roots without
the record and Workbench initialization retain their existing behavior.
These entry points do not provide menu integration or a managed uninstaller.
Pixi/tool management stays internal; data remains outside immutable program
generations and independent of the downloaded launcher location.

Platform wrappers consume the same release identity and installation/lifecycle
contracts. They show progress and errors and open the shared browser application;
engines, provider setup, jobs and document UI keep one implementation. Use a small
native adapter over the existing lifecycle. The adapters use bundled
portable Python/Tk for its small setup window, with a private stdin handoff for
first-administrator credentials. Its actual library/FUSE prerequisites need
testing; AppImage does not imply offline setup or
universal Linux compatibility. Publisher-download payloads reconstruct the runtime
from hash-pinned package downloads; standard managed bundles are also supported.

The Python install/update/rollback lifecycle exposes opt-in versioned events and
cooperative cancellation. Ordinary observer errors do not interrupt initialization
or recovery; terminal outcomes follow cleanup and ownership release. Journals and
recovery fences, rather than the event transport, remain durable authority.
Generated native entrypoints emit the same opt-in stream before Python, then
continue the operation ID/sequence through managed adoption. An exclusive terminal
handoff is validated and published after outer cleanup. Native publisher payloads
report their dependency/verification/environment substeps through that same outer
stream. A shared native transport generator handles carrier and publisher download
cancellation, with caller-owned admission, progress and terminal policy. Native
unpacking still finishes its current step. The installed-Python updater
consumes the same publisher records under its existing lifecycle identity and
keeps both maintenance leases until the child is reaped, including cancellation
and observer interruption. Explicit retained-adoption retry binds the original
transaction, authenticated inputs and program/data roots before reconciliation.
Pointer and receipt evidence distinguish committed activation from failed or
unknown outcomes before error recovery. Native entrypoints reserve their program
lease before unpacking and bind each claimed generation to its authenticated
release and canonical roots before handing it to the manager. Failed cleanup and
quarantine preserve generations with uncertain or foreign ownership. Reruns can
reuse a matching ready generation; the manager selects and validates any retained
adoption transaction under both leases. Opt-in preparation for standard bundles
and Documents publisher payloads uses a separate lease-retaining custodian and
native process-set custody: a Linux user
scope/cgroup or a Windows job assigned at process creation. A claim/identity/root-
bound completion records native extraction only. Publisher custody also validates
the complete private result protocol before publishing that record; its file-backed
progress observer feeds the existing outer event owner without owning custody.
Resume still checks the profile and runtime, persists the first root-binding
intent, then hands off to normal
managed adoption. A matching nonzero completion can now retire an unadopted
failed preparation intact under the original program lease, after fresh release
verification. A new claim then starts extraction again. Current/previous pointers,
bindings, receipts or manager-owned state preserve the generation for managed
recovery or operator inspection. Unknown custody and old preparing claims preserve
generation and scratch; parent exit, a free lease or a missing job name cannot
prove completion. Unattributed scratch is not reclaimed.
Windows compiles the trusted helper with its Framework compiler through an owned
UTF-8 response file. The controller and custodian load the same assembly bytes;
compilation and its private diagnostics remain inside preparation scratch.
Outer event-mode downloads can stop
during a transfer; scratch cleanup follows curl reaping or closure of the
PowerShell-owned output stream. This does not terminate unpacking or activation.
The Linux graphical adapter consumes this interface. See the
[update contract](update-and-recovery.md#installation-event-interface).

Both compositions reuse profile-driven onboarding components. Native model
preparation, connection setup and readiness should become one
resumable user flow. Updates retain data/root/key identity; uninstall should remove
only owned program/integration objects and preserve user data by default. These
are targets, not claims about the current script installers.

## State and execution

One managed runtime owns each data root. Program trees are immutable; databases,
uploads, jobs, queues, exports, secrets and recovery records live in owned data
paths. Documents and Workbench have separate root profiles, ports and cookies.
Fresh Documents ordinary content is plaintext; credentials are protected. Existing
encrypted roots retain their recorded policy and key lineage.

Both direct and passive workflows use the same semantic document/translation
contracts. Direct work requires configured providers; passive MCP claims hydrated
packets and supplies model completions without host provider credentials. Owner,
source revision, lease, schema and idempotency checks govern acceptance. Final
publication and export checks are distinct from accepted packet submissions.

Extraction input and assembled document HTML have distinct note vocabularies.
New passive PDF runs freeze the `footnotes-v1` input policy; page preflight and
worker validation enforce its marker/body shapes before assembly. Older passive
packets retain their original contract. Compact work delivery preserves the small
versioned extraction contract while referencing omitted instructions. A worker
must retrieve and read missing instructions after context loss; hashes establish
identity, not that the executing model still has their contents.

The [EPUB projection](epub-export.md) is shared by direct extraction and translated
semantic export. A named policy separates bounded edition choices from canonical
source metadata, with owner/revision-bound preflight and immutable delivery
receipts. Archive validation is add-on-owned and independent of Flask routes;
formal accessibility assertions require an edition evaluation rather than a
renderer heuristic. Historical field-absent requests retain their policy.
Each product owns saved edition recipes in separate, owner-scoped ORM rows under
the root content policy. Version and source witnesses govern updates; tombstones
preserve concurrency history. These recipes never rewrite canonical documents,
translation plans or historical delivery receipts. A source change retires content
decisions while keeping general export preferences.

Common semantic image descriptions are bounded, inert and translatable content,
validated by `carlina.core.semantic_images` in ingestion, target reconstruction
and semantic export. The diagram projection retains authored descriptions and
derives complete structured topology alternatives from the validated graph.
Source captions and frozen passive prompt policies remain separate from generated
accessibility adaptations and evaluated edition claims.

See [extraction](extraction-efficiency.md), [Translator](translator.md),
[MCP](mcp-integration-architecture.md), [add-on API](addon-api-v1.md),
[content policy](adr/0011-root-bound-ordinary-content-policy.md) and
[distribution decision](adr/0012-documents-agpl-distribution.md).
