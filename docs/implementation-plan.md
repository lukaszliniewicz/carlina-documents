# Documents release plan

Last reviewed: 2026-10-29

Prepare a useful public project in small releases. The shared runtime, document
engines, browser interface, script installers and application-source delivery
already exist. The [ledger](project-ledger.md) distinguishes that evidence from
unfinished release work. Component contracts describe implemented behavior;
completed implementation diaries remain in Git history.

The source preview is published as `v0.2.0a1`. The next release work is Linux
installer/AppImage qualification and public download preparation. Resolve the
new public CI portability failures before advertising packaged downloads.

## Release scope

| Milestone | What it promises | Required before release |
|---|---|---|
| Source preview | A documented developer checkout and build | Reviewed public tree and history, working contributor setup, source/build checks and original notices |
| Native installer preview | A technical-user installation using the existing shell/PowerShell entry points | Exact candidate on the advertised Windows/Linux targets, source/notices for its actual payloads, public downloads and existing release verification |
| Desktop beta | Per-user setup and everyday launch without terminal commands | Small platform wrappers, guided first use, owned integration/uninstall and a cold-machine trial per advertised platform |

Publish the source preview first. A source release does not wait for graphical
installers. The native preview may retain documented setup commands and manual
product activation; call those limitations out. Desktop convenience follows when
its implementation is useful, rather than blocking every earlier release.

Windows executables will be **unsigned**. Authenticode certificates, commercial
signing services and reputation building are outside this plan. Release-index
signatures and artifact hashes remain part of the existing installer/updater
contract. Record the unsigned status in download instructions and test the actual
installation experience before advertising the desktop beta.

The initial binary route uses the already exercised **standard managed bundles**.
Their download size is an acceptable starting tradeoff. The alternative publisher
reconstruction payload is implemented but remains experimental until its complete
artifacts are qualified. Its qualification does not block a release that does not
ship or select it. Review the selected bundle's actual redistributed components;
an earlier publisher-payload review does not cover a different artifact.

## 1. Make the source repository publishable

1. Keep a short README, user entry point, contributor setup, security reporting,
   architecture, current plan and current ledger. Remove superseded phase diaries,
   machine-specific acceptance details and references to unrelated private work.
   Retain useful component contracts and original copyright/licence notices.
2. Review every tracked publication input and the history that would become
   public. Removing a file from the current tree leaves earlier commits intact.
   Keep private operational evidence outside the public repository; publish a
   reviewed fresh-history snapshot. The maintainer selected this route: retain
   the original history privately and preserve Workbench's old pins; start the
   public repository with a reviewed source snapshot and no old parent commits.
3. From an isolated export of the shared repository, follow contributor setup,
   run the offline/static/frontend checks and build Documents without the private
   checkout. Validate local links in both the checkout and selected source archive.
   Credentials, user documents, runtime state and build caches stay outside Git.
4. Confirm the source licence and attribution statements against the selected
   inputs. Carlina's MIT notice stays; the assembled application follows
   [ADR 0012](adr/0012-documents-agpl-distribution.md). Review required component
   source/notices for binaries separately from the source-only milestone.

Exit: a reviewed source snapshot with honest setup/support instructions, an
independent contributor build and no known private publication inputs. Preserve
the old private repository and recovery bundle before the authorized fresh public
repository is created. Current-tree checks do not qualify the old Git history.

## 2. Release the existing native installer path

- Choose one public preview version, freeze its clean source revision and build
  the application wheel/source and standard Windows/Linux bundles. Preserve
  manifests, locks, hashes, component inventory and required notices/source.
- Keep exact application source obtainable through the installed browser/MCP
  offer and retained release downloads after newer versions appear. Use the
  existing source/notices tooling; resolve actual gaps rather than building a
  second compliance system.
- Use versioned GitHub release assets and one documented preview channel. Retain
  the existing signed-index/profile/hash admission, with its public verification
  key in the trust store and the private key outside Git. A protected local
  signing step is sufficient initially. A new release pipeline, attestations,
  signing service or automated key-rotation system is optional follow-up work.
- Exercise the exact candidate on Windows 11 x86-64 and the declared Linux x86-64
  baseline: install, initialize, prepare the three products, complete a small
  workflow, restart and retrieve matching source. Test one upgrade/recovery path
  with synthetic data and rejection of altered/wrong-profile release inputs.
- State tested paths, prerequisites, disk needs and known limitations. Use the
  default install locations first. A discovered unsupported deep custom path can
  be documented or refused; exhaustive path/platform combinations are not a
  preview prerequisite. Do not claim clean-machine or graphical acceptance from
  the existing-host script tests.

Exit: the exact selected artifacts install and perform their advertised workflow,
the required source/notices are available, and download/setup instructions match
the preview's limits. The current `uninstall` command is informational; document
safe manual removal for this preview without promising a managed uninstaller.

## 3. Add desktop convenience

Build a small per-user NSIS Windows setup adapter over the existing lifecycle.
Linux uses an AppImage, as selected for convenient download-and-run delivery.
Its small Python/Tk bootstrap carries setup/launch UI and the frozen native
installer; the actual managed runtime and document data live outside the AppImage.
The existing installer owns verification, preparation, adoption and recovery.
Keep document engines, settings and browser UI shared. Qualify this one adapter
rather than adding a packaging/toolkit comparison programme.

The Linux adapter includes graphical progress, cooperative cancellation and
first-administrator creation through a bounded private stdin pipe. An already
configured installation is opened through its bound launcher. Existing-host
AppImage launch and graphical first-account/relaunch/source checks now pass as
recorded in the ledger. Public inputs, desktop integration/update/uninstall and
cold desktop acceptance remain open. An explicitly labelled technical AppImage
preview can precede the desktop beta, with manual product activation, launch and
removal instructions. Public artifact verification and actual notice/source
coverage still gate that preview.

The desktop beta needs:

- useful default paths, progress, actionable failure/retry and cancellation at
  existing safe boundaries;
- a shortcut/menu entry in an owned stable location, with relaunch reconnecting
  to the existing runtime and no global PATH changes or routine elevation;
- first administrator creation without passwords in arguments, URLs or logs,
  followed by shared browser setup for product activation and either a direct
  provider or passive MCP client;
- uninstall of receipt-owned program/integration objects, retaining documents,
  translations, credentials and recovery material by default; data erasure is a
  separate explicit action; and
- one cold Windows and one supported graphical Linux trial without developer
  runtimes or warmed caches, including unsigned Windows launch, interrupted setup,
  relaunch and uninstall/reinstall with a synthetic project retained.

Test the pinned AppImage runtime's actual FUSE mount and extract-and-run paths,
plus the graphical prerequisites. Document the supported route and fallback;
do not promise universal Linux portability. Preserve the original portable
Python and runtime notices with the AppImage and its source/build inputs.

Exit: a first-time user completes a small workflow without terminal/config edits
or manual add-on activation. Make accessibility and platform claims only to the
extent actually tested. A larger usability study is later work.

## Compatibility and acceptance

Workbench remains a first-class consumer of this source. Preserve its imports,
add-on IDs, ORM/migration lineage, model identity, profile routing and root/key
policies. It promotes reviewed pins and owns its own compiled assets, private
add-ons, immutable program generations and mutable data root. Source cleanup
never deploys or migrates either installed application.

Use focused checks while editing and one full affected lane at a coherent code
checkpoint. [Engineering policy](engineering-policy.md) defines the blocking
gates. Documentation edits need link/namespace and affected packaging checks;
they do not need another native installation or whole-book extraction. Keep the
existing source/dependency/closure ratchets; do not relax them for presentation.

For a binary candidate, reuse unchanged engine evidence and run a small synthetic
or licensed workflow corpus through the installed application: PDF extraction,
EPUB/DOCX ingestion, segmentation, aligned translation and four-format delivery.
Inspect structure and note links, run the configured EPUB validator, and check
representative PDF/DOCX output. Accepted packets are distinct from final products.
Preserve revision/owner/lease checks, historical frozen requests and source text.
Direct-provider support claims require an actual configured test; disclose any
unverified path. Accessibility assertions require edition evaluation as defined
by the [EPUB contract](epub-export.md).

Keep one current ledger and plan. Store detailed operational receipts privately;
retain release-bound public source, manifests and notices with their artifacts.
Run each expensive check when its boundary changes or conflicting evidence needs
resolution, rather than restarting every matrix after prose edits.

## Deferred work

Paid Windows code signing, offline bundles, publisher reconstruction as the
default, elaborate release automation/attestations,
independent host/product packages, namespace/schema redesign, hostile third-party
add-on isolation, server deployment, GPU segmentation, macOS/ARM and a broad
provider/platform/accessibility certification programme do not gate these first
releases. Existing safeguards and tests remain; new work must serve an observed
need. The next action is source/publication hygiene and independent source-preview
qualification, followed by the selected standard-bundle preview.
