# Documents project status

Last reviewed: 2026-10-28

The [release plan](implementation-plan.md) owns sequencing; component contracts
and tests own behavior. Release assets identify their own source revision,
checksums and tested platforms. Superseded implementation diaries stay in Git.

## Current scope

Version **0.2.0a1** begins with a source preview. The maintainer has selected a
fresh public history and will retain the earlier repository privately. Private
Workbench continues to consume a reviewed shared-source pin with its own add-ons,
configuration, compiled assets, program generations and data root.

| Area | Implemented behavior and remaining limit |
|---|---|
| Documents | Document Processor, Translator and Sentence Segmenter share the host, browser application and authenticated MCP |
| Workbench compatibility | Shared imports, add-on IDs, migrations, profile admission and root/key policies are preserved; composition pins are promoted explicitly |
| Script installers | Standard Linux/Windows bundles have existing-host installation, initialization and exact installed-source evidence |
| Linux AppImage | FUSE and extract-and-run launch, graphical install/account creation and relaunch have existing-host evidence; the candidate uses test download/trust inputs |
| Release verification | Signed release indexes, artifact hashes and application-profile admission are implemented; permanent public download/trust acceptance remains open |
| Source delivery | The application wheel embeds matching source; installed browser HTTP and authenticated MCP provide version/hash-bound retrieval |
| Publishing | Shared notes, review, edition recipes, covers, crops and EPUB/DOCX/HTML/PDF export are implemented; accessibility claims remain edition-specific |
| Desktop convenience | Guided product preparation, menu integration, managed removal and cold-machine trials remain open |

## Source qualification

An isolated shared-source checkout installs its locked development environment
and passes the Python audit and frontend checks without the private project.
Tracked-input review excludes user data, credentials, environments, private
operations records and earlier Git history. The document reference template has
the stock Pandoc body and metadata, with the project's retained style changes.
These bounded checks do not certify arbitrary user documents or future inputs.

The release preparation updates Vite to 8.2.4, Vitest to 4.1.12 and Undici to
8.12.3. The patched standalone frontend passes build, lint, checks and tests;
Workbench passes its composed build, all 30 workspace checks and all seven test
workspaces. npm reports no high or moderate findings and two low-severity
upstream development-tool findings without a patch. The existing audit threshold
has not changed.

The affected packaging/source lane passes 97 tests; the AppImage controller and
entrypoint lane passes 23. The builder now preserves the notice catalogue's
relative paths. Earlier engine and full offline evidence supports unchanged
behavior, but does not qualify newly assembled public binaries. Blocking static,
typing, dependency, import-boundary, dead-code and closure gates remain in force;
current policy files own their budgets.

## Binary release limits

The existing standard bundles and AppImage were tested on existing hosts with
synthetic roots, local transport and a test signer. The graphical trial uses a
controlled browser handler. Its cleanup required a separate stopped-state
readback after the controller encountered an exited child. This is neither a
cold-machine trial nor an accepted public download or complete installed workflow.
Exact operational receipts remain outside the public project.

The inspected Linux bundle contains 108 Conda packages and 100 Python wheels,
including Carlina itself. All 207 third-party archives match the exact selected
lock set and hashes. Original package notices, recipes and archive identities are
retained for binary review. Missing wheel notices and required component-source
coverage must be resolved before publication. The broader lock inventory also
includes optional workers and models; it is not proof that those are bundled.

The [bootstrap catalogue](../third_party/appimage/NOTICE.json) records original
portable-Python and AppImage runtime notices and upstream provenance limits.
Build-only tools are retained privately with hashes and source references;
redistributing those tool binaries is unnecessary for the application preview.
Upstream floating build inputs are stated as limits, without inventing a
bit-for-bit rebuilding requirement for permissively licensed libraries.

Windows executables will be unsigned. Paid signing and an additional release
system are outside this plan. A technical AppImage preview may use documented
manual product activation and removal before the desktop beta, once its exact
public download, verification, source and notice inputs pass.

## Next actions

1. Finish the 0.2.0a1 source build and fresh-history publication, retaining the old
   private repository and all Workbench recovery pins.
2. Complete actual binary notices/source, stable release-key and versioned URL
   inputs; build the Linux preview from that public source.
3. Exercise the exact installed workflow, source retrieval and public download
   hashes before advertising the AppImage. Broader desktop convenience follows
   the [release plan](implementation-plan.md).

No source preparation step deploys, migrates or restarts an installed application.
