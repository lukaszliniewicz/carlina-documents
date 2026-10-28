# Documents application

Carlina Documents runs Document Processor and Translator through the same engines
used by private Carlina. This repository owns their source, shared host and UI;
private Workbench consumes a pinned revision. Documents uses native workers and
prebuilt browser assets. It keeps PyMuPDF under the
[AGPL distribution decision](adr/0012-documents-agpl-distribution.md).
The [packaging and source-delivery requirements](#maintainer-packaging-and-source-delivery)
describe the remaining publication gates.
Direct provider UI calls remain best effort; authenticated MCP and passive
workflow acceptance remain required.

## Current launch and first use

The current native online installers are shell/PowerShell scripts. The initial
release path is a source preview, then a technical native-installer preview using
standard bundles. Linux desktop delivery uses a small AppImage setup/launch
adapter; its source prototype is undergoing native qualification. An unsigned
Windows setup EXE and complete desktop integration follow later. The commands below describe the implemented
maintainer/source path. See the [release plan](implementation-plan.md).

There is no accepted public release channel yet. For maintainer previews, use the
exact signed preview manifest supplied with the reviewed artifact. The current
stable-channel fallback selects the Documents repository for Documents and the
existing Workbench repository for Workbench. Explicit manifest/environment
sources retain precedence, and the profile guard rejects an incompatible index.
Public source/trust delivery and installed-candidate acceptance remain release
gates; correct routing does not create public release assets.

The Python `install`, applying `update` and `rollback` commands also support opt-in
`--events-jsonl`, with a closed [event schema](../carlina/core/installation-event-v1.schema.json).
`--cancel-file PATH` requests cancellation at safe boundaries. Once target code
can access mutable data, the manager finishes activation or recovery before its
terminal event. Generated shell/PowerShell installers also offer an opt-in
noninteractive adoption stream, correlated through Python and completed after
scratch cleanup. Their event mode skips the separate interactive setup and PATH
integration. Publisher substep progress and explicit retained-generation retry are
implemented in source; complete publisher payloads remain experimental. The
AppImage adapter consumes this stream and creates the first administrator over
private stdin. Full desktop onboarding remains open. The shared source's update guide
defines the handoff and its limits.

Source-checkout commands require a dedicated development `CARLINA_HOME` and free
HTTP/MCP ports. Do not point them at a root owned by an installed or running
application. The generated installed launcher uses its recorded managed binding.

Use the repository's pinned Pixi version and locked `documents` environment:

```text
pixi run --locked -e documents python -m carlina.documents init
pixi run --locked -e documents python -m carlina.documents start --open-browser
pixi run --locked -e documents python -m carlina.documents status --json
pixi run --locked -e documents python -m carlina.documents doctor --json
```

The installed entry point is `carlina-documents` (the generated
`carlina-documents.ps1` launcher on Windows). Use the generated installed launcher
for initialization/start/stop; it supplies the managed Pixi path and isolates
user-site Python packages. It fixes the application profile to Documents for
startup, diagnostics and maintenance. Its default listener is
`http://127.0.0.1:5005`; its loopback MCP listener starts on port 5006. Full
Carlina retains ports 5003 and 5004, so the two listener pairs can coexist.
An explicit `--port` or `CARLINA_PORT` overrides the HTTP default, and
`CARLINA_MCP_PORT` overrides the MCP default. Session and remember cookies are separate
so both applications can be used in one browser.

Initialization retains the shared administrator setup. Activate the three products
through Settings → Add-ons, reviewing their declared code access and dependency
notices. The home page displays available products and links to configuration
when activation is needed. Model connections are configured through Settings.
Passive MCP extraction and translation do not require a configured direct API
provider; direct execution does.

For passive work, sign in as an administrator and select **Connect AI client**
on the home page. The panel reports the running listener's actual URL. Give the
connection a name and optionally authorize an existing source folder on this
computer. Without a folder, the client can work with documents already imported
into the account. Copy the URL and one-time bearer token into a client that
supports MCP over HTTP. Save the token before closing the panel; Carlina stores
only its digest. The same panel lists and revokes this account's connections.

Each connection has document, translation and passive-work scopes. Imports must
stay inside its authorized folders. Ask the client to import and extract a small
document first, inspect its completed revision and boundary repairs, then
translate or export it. The client declares its input/output budgets and
capabilities before passive work starts, claims prepared packets and submits
model responses. Carlina validates, retains recovery evidence and publishes the
result. A submission receipt alone does not mean that the final document is ready.

The listener is local-only. `CARLINA_MCP_ENABLED=0` disables it at startup;
`CARLINA_MCP_ENABLED=1` explicitly enables it. Changing this setting requires an
application restart. The full Workbench profile retains opt-in MCP startup.

### Optional EPUB covers

Document Processor and Translator use the same EPUB cover renderer and packager.
The export dialog defaults to the existing cover. For EPUB, an owner can instead
choose a physical PDF page or generate a static title-and-author cover. Generated
Translator covers use the translated edition's title, subtitle, authors and
translators. A PDF cover retains the source page's original language.

PDF metadata may suggest `cover_candidate: {page, kind, evidence}` or null. The
page is counted from 1, including unnumbered front matter. Only inspected pages
can support a suggestion; it never changes export defaults. New passive runs
freeze `mcp_cover_candidate_policy=pdf-page-v1`; retained runs without the marker
keep their original schemas and prompts.

Both MCP export tools accept `epub_cover_mode` (`existing`, `pdf_page`, or
`generated`) and `epub_cover_page` for the PDF choice. Cover options are bound to
the retry fingerprint. They do not rewrite source documents, saved edition
details or translation revisions. HTML, DOCX and PDF exports retain their
existing cover behavior.

Selected covers become one raster `cover-image` (PNG by default) and a separate
opening spine page, without reader-side scripting. Rendering uses owned uploads
only, rejects
password-protected, missing and out-of-range PDFs, and bounds input to 512 MiB
and output to an 1800-pixel longest edge. Translator checks the current owned
source's opaque HTML projection against its frozen snapshot. New snapshots also
freeze the PDF SHA-256; older snapshots use the owned PDF when the HTML witness
still matches. Explicit replacement retires the original cover's sole image
resource while preserving body text, note identities and links.

### Revising accepted image crops

Completed PDFs offer **Adjust image crops** in the document actions menu. The
editor reopens accepted coordinates against a retained page image, including
images accepted automatically. Saving replaces only the selected image assets
in the current document, derives its Markdown and publishes the new Knowledge
version. It preserves current text, captions and note links without model calls
or another extraction job. Future exports use the revised crops; existing
translation snapshots and exported files retain their earlier image assets.

Older output may lack source coordinates. Its saved image can be trimmed, or the
owner can select a physical PDF page and draw a replacement crop. Newly saved
crops retain that source for subsequent expansion and adjustment. Sources and
generated assets stay within the owned document directory. The editor submits
only coordinates, source-page choices and its required document revision;
concurrent changes require a reload. Initial image review retains its separate
post-extraction checkpoint and resume flow.

Production uses prebuilt assets. Node is a development/build dependency. Native
document conversion and SaT segmentation use their declared, locked managed
environments; Docker, WSL, office software and LaTeX are not requirements of
this composition. Activate Sentence Segmenter before aligned-sentence source
planning and prepare its pinned model assets before offline segmentation.
Activation ignores explicitly on-demand container alternatives when the host
excludes container services; required container units still block admission.
Fresh minimal Linux userland and Windows installations with a restricted process
PATH have exercised default native workers and CPU segmentation. This is accepted
native preview evidence, with whole-machine VM testing deferred. Whole-book
linguistic quality remains separate evidence.

## Data and configuration

Documents uses separate platform data, configuration, cache, logs and secrets
under the application name `Carlina Documents`. It does not adopt checkout-local
databases or load the checkout's `.env` or optional ignored Python configuration.
The canonical configuration file and explicit process environment remain usable.
In a source or full distribution, the runtime profile is selected by the entry
point, command argument or process environment; a dotenv file cannot switch it.
A Documents distribution defaults to its immutable profile and rejects a
Workbench override before resolving or creating a data root.

`CARLINA_HOME` selects an explicit portable root. `CARLINA_SECRETS_DIR` and
`CARLINA_ADDON_ENVIRONMENTS_DIR` retain their shared-runtime meanings. The latter
allows a shorter owned worker-environment path on Windows. The default worker
path has native acceptance; other path lengths remain bounded compatibility cases.

A root records its application profile and content policy before durable content
writes. A conflicting profile is refused. Fresh Documents roots use plaintext
ordinary document/translation content and protected credentials. Existing
encrypted roots keep their policy and key lineage; selecting Documents does not
decrypt them. See [ADR 0011](adr/0011-root-bound-ordinary-content-policy.md).
Backups retain the profile marker. Encryption keys remain a separate recovery
requirement for encrypted content and protected secrets.

## Maintainer packaging and source delivery

The source selector includes the shared runtime and only `doc_processor`,
`translator` and `sentence_segmenter`. It rejects mutable data, environments,
secrets, symlinks and unrelated add-on trees. Static assets and build inputs are
an explicit additional inventory. The builder exports a clean Git commit,
stages only those members and adds a closed distribution descriptor containing
the profile, selected add-ons, source revision and commit timestamp. It produces
normalized wheel and source archives. The wheel also bundles the exact normalized
application source archive with its version, revision, byte length and SHA-256.
`--wheel-only` still builds and embeds source.
An online pack can contain only that application wheel and environment metadata.
The dependency-source metadata wheel is optional; preparation and bootstrap
generation reject undeclared wheels and omit absent companion fields.
From this repository's locked development environment:

```text
pixi run --locked python scripts/build_documents_release.py --root <clean-checkout> --revision <commit> --output-dir <artifact-directory>
pixi run --locked python scripts/verify_release_artifacts.py --application-profile documents <artifact-directory>/*.whl <artifact-directory>/*.tar.gz
```

The source archive contains the corresponding frontend source and prebuilt
assets, together with the selected recovery guide and its linked shared
documentation. Local documentation links must resolve within that explicit
selection. The release builder uses its generated `carlina/distribution.json` to
compile only the Documents core routes, using the pinned npm lock. Its temporary
npm dependencies are removed before source/wheel packaging. Building a release
therefore requires Node 24 and npm 11 as well as the locked Python environment;
installed users receive the prebuilt interface.

The Linux AppImage is an online bootstrap, not an offline application bundle.
It carries portable Python/Tk, the small setup/launch adapter and a generated
Documents installer. That installer downloads the standard managed runtime;
documents and installed generations live outside the AppImage. Existing configured
installations are reopened through their own launcher. The current adapter does
not upgrade an existing installation or add menu entries. Removal and updates
still follow the [managed-runtime guide](update-and-recovery.md).

Build it on Linux x86-64 from a clean, committed checkout with the same version as
the generated installer. Supply local, hash-verified inputs; the builder does not
download tools or choose a moving runtime implicitly:

```text
pixi run --locked python scripts/build_documents_appimage.py --root <clean-checkout> --revision <commit> --version <version> --output-dir <artifact-directory> --installer <generated-installer.sh> --installer-sha256 <sha256> --index-sha256 <signed-index-sha256> --python-archive <portable-python.tar.gz> --python-sha256 <sha256> --appimagetool <appimagetool.AppImage> --tool-sha256 <sha256> --runtime <type2-runtime> --runtime-sha256 <sha256>
```

The builder uses zstd compression and binds its copied source, original notices,
input hashes and source revision to the output receipt. Pinned bootstrap inputs
and outstanding binary/source obligations are recorded in
[the bootstrap catalogue](../third_party/appimage/NOTICE.json). Test the actual
AppImage on the declared desktop baseline. Where FUSE mounting is unavailable,
the runtime's `--appimage-extract-and-run` path needs separate acceptance; its
existence alone does not establish graphical or clean-machine support.
The supervised Linux installer currently requires systemd 254 or newer with a
working user session and its standard command-line prerequisites. The Tk window
uses X11 or XWayland. Declare the tested desktop baseline explicitly; these
requirements are not removed by wrapping the installer in an AppImage.

New packaging inputs include the complete AGPL text and the distribution
decision; older immutable previews retain their original contents. Carlina's MIT
notice describes its own source, and dependencies retain their separate terms.
The exact pinned model cards and [asset notice mapping](../third_party/models/NOTICE.json)
are included in the source archive and wheel's licence directory. Explicit
preparation downloads inference files directly from the publisher. These notices
preserve the upstream MIT declarations; the pinned repositories provide no
standalone licence text. Updating notices does not invalidate prepared models.
To rebuild those assets after extracting the archive, put Node 24 and
npm 11 on PATH and use its locked Documents environment from the archive root:

```text
pixi run --locked -e documents python -c "from iarf_assistant.core.frontend_orchestration import build_frontend_workspace; result = build_frontend_workspace('.', force=True); assert result['success'], result['message']"
```

This helper rebuilds the core and three selected add-on frontends and writes
their runtime build stamps. The archived immutable descriptor selects the same
Documents route module; a descriptor-free shared checkout builds Workbench.
Direct Vite commands alone do not regenerate those
stamps. Asset hashes can differ between build paths; each build must retain its
matching assets, manifest and stamps together.

The separate legacy core stylesheet, `output.css`, uses
the pinned standalone Tailwind CLI. New source archives include
`scripts/build_tailwind.py`; from the archive root, rebuild it with
`python scripts/build_tailwind.py --minify`. The builder downloads the declared
platform binary and verifies its SHA-256 before execution. Older previews retain
the generated stylesheet but omitted this builder; they do not establish this
part of corresponding-source coverage.

To build a wheel with the archived prebuilt assets or the newly stamped assets,
run `python -m pip wheel --no-deps .` with Python 3.14. Attach the retained archive
to the resulting unpublished wheel with:

```text
python scripts/package_application_source.py --wheel <rebuilt-wheel> --source-archive <retained-source-archive>
```

This verifies matching version/revision metadata and packaged source/asset bytes,
then regenerates wheel checksums. The archive does not require the full private
checkout or its Git history. Changed application code requires its own matching
source artifact.

New Documents builds show **Source code** in the application shell. The public
`/api/application-distribution` projection identifies the installed version,
revision and verified application archive. Its hash-bound download works without
login. MCP status exposes the same identity, and authenticated clients can read
the exact bytes from `carlina://application/source/<sha256>`. Missing or tampered
bundles produce no usable source offer. Older previews remain runnable without
this metadata; neither the private checkout nor user data is a source fallback.
This archive covers the application. Applicable native dependency source and
notices remain separate publication gates.
Native packs can additionally carry a verified, version-bound companion manifest.
When present, **Dependency sources** and **Third-party notices** appear alongside
**Source code**. `/api/application-distribution` and authenticated MCP resource
`carlina://application/companions` expose download URLs, byte lengths, SHA-256
hashes and platform inventory bindings. These are selected companions; discovery
does not certify complete licence or source coverage. The app does not proxy or
install the large ZIPs. Invalid companion metadata hides those links while the
independent application-source download remains available.

Run the read-only inventory tool from the current shared checkout or a source
archive that includes it. Older preview archives remain valid inputs:

```text
python scripts/generate_documents_inventory.py --source-root <unpacked-source> --wheel <matching-documents-wheel> --platform linux-64 --output-dir <new-report-directory>
```

Use `win-64` for the Windows lock selection. The command verifies packaged
manifests, worker locks and model descriptors against the wheel and writes
`inventory.json` and CycloneDX 1.6 `cyclonedx.json`. It records separate base,
host-vendor and native-worker contexts, pinned model assets and the full frontend
build lock, plus the embedded application source identity and checksum when
present. It neither imports application code nor downloads dependencies.
Frontend bundle membership, installed-package notices and corresponding-source
obligations remain separate release checks; these reports do not certify public
distribution. Pip lock hashes are allowed artifact alternatives, not proof of
which artifact was installed.

Collect Conda notice candidates for the same artifact's resolved inventories
with Python 3.14:

```text
python scripts/collect_documents_notices.py --inventory <linux-inventory.json> --inventory <windows-inventory.json> --cache-dir <archive-cache> --output-dir <new-notice-directory>
```

The collector downloads the exact locked Conda-forge archives over HTTPS and
verifies their SHA-256 hashes before reading them. It supports both `.conda`
and `.tar.bz2`, including notices placed in payload licence directories with
nonstandard filenames. It retains
unmodified notice, metadata and recipe bytes with their archive paths and
environment references in `index.json`. It neither installs nor executes package
contents. Verified archives can be reused after an interrupted collection; use
a new output directory. The default download budget is 2 GiB.

Collection records missing package notice candidates as gaps and distinguishes
recipe notices from package notices. It does not accept licences or complete
the separate installed PyPI, frontend, model and corresponding-source checks.

After reviewing those gaps, prepare a supplement directory with a `manifest.json`
of format `carlina.documents-conda-notice-supplement`, schema version 1, the exact
collection index SHA-256 and one decision per discovery gap. Each decision binds
an archive hash, an explanation and one of three bases: a metadata-only wrapper,
an embedded header notice or a notice from an exact sibling package. Supplement
blobs use `files/<sha256>` with their original archive path, size and origin
archive hash. Sibling notices must also match the collection's original bytes;
the reviewed explanation records their source relationship. Wrapper decisions
record the operator's payload review rather than claiming a new archive scan.

Assemble a portable notice directory offline:

```text
python scripts/assemble_documents_notices.py --collection-dir <verified-collection> --supplement-dir <reviewed-supplement> --output-dir <new-notice-bundle>
```

The assembler checks every retained input member, preserves notice bytes and
requires the exact gap set. Its deterministic index retains package, environment
and artifact provenance without workstation paths. It verifies the input evidence
and mappings; licence acceptance, native source, installed PyPI, frontend and model
coverage remain separate publication checks. This release-preparation tool requires
POSIX directory-descriptor operations and has Linux acceptance. Its output is
portable; the installed application and Windows installer do not run the assembler.

Prepare selected dependency sources as a separate release companion:

```text
python scripts/assemble_documents_sources.py --manifest <reviewed-source-manifest.json> --files-dir <source-blobs> --inventory <linux-inventory.json> --inventory <windows-inventory.json> --output <new-source-companion.zip>
```

The closed input format is `carlina.documents-source-companion-input`, schema 1.
It binds the Documents version, revision and wheel hash to exact inventory hashes;
component selections reference allowed binary hashes and named source, recipe and
notice members. File records specify portable destination names, kinds, SHA-256,
byte lengths and original URL/archive/member provenance. Input blobs are regular
files named by their hash; include at least one recipient instruction file.
The assembler streams original bytes into a deterministic ZIP, rejects mismatched
or unsafe inputs and refuses an existing output. Its embedded index records
selected coverage and keeps source-closure/public acceptance false. This is a
POSIX release-preparation tool; recipients use the portable ZIP. Release hosting,
final native delivery and full source coverage remain separate gates.

After finalizing the application wheel, inventories and selected companions,
prepare a metadata-only discovery wheel:

```text
python scripts/package_application_companions.py --wheel <documents-wheel> --manifest <reviewed-companion-manifest.json> --output-dir <new-directory>
python scripts/build_managed_pack.py --wheel <documents-wheel> --companion-wheel <metadata-wheel> --platform linux-64 --output-dir <new-pack-directory>
```

Use `win-64` for the corresponding Windows pack. The closed manifest format is
`carlina.application-companions`, schema 1, profile `documents`. Its identity
fields are `application_version`, `source_revision`, `application_source_sha256`
and `wheel_sha256`. `inventory_inputs` contains unique `linux-64` or `win-64`
records with `platform` and `sha256`; `companions` contains at most one of each
`dependency_sources` and `dependency_notices`, with `kind`, `filename`,
`byte_length`, `sha256`, `url` and `coverage: "selected"`. At least one inventory
and download is required. URLs use HTTPS; numeric-loopback HTTP is allowed for
local preview delivery. Credentials, query strings and fragments are rejected.
The pack builder verifies the metadata wheel against the exact application wheel
and target inventory before packing and rejects persistent input changes before
publishing. The signed pack covers the injected metadata; a mutable release
receipt is not its trust root. Older packs remain runnable without companions.

The wheel retains the canonical `carlina` package name. Install it into an
exclusive environment; full Carlina and Documents do not share a Python prefix
or immutable program tree. Managed packs select the locked `documents`
environment, prebuilt browser assets and profile-specific launchers. Signed
indexes bind the profile, and bootstrap/update/rollback validate the unpacked
descriptor before importing application code. Descriptor-free full and legacy
packages preserve their Workbench behavior.

Prepare the online bootstrap inputs from a matching managed pack and platform
inventory. Build the companion metadata, managed pack and bootstrap from the same
source tree. For an exported Documents build, run those controls from its exported
tree: the companion validator compares the exact project licence bytes, including
line endings. Source export normalizes those endings while preserving Carlina's
MIT notice and the separate AGPL text.

```text
python scripts/prepare_publisher_bootstrap.py --artifact-metadata <pack>.artifact.json --inventory <platform-inventory.json> --output-dir <new-preparation-directory>
```

The command verifies the original pack and produces `plan.json`, a deterministic
`skeleton.tar.gz` and `preparation.json`. The skeleton retains the application
wheels and environment metadata while replacing publisher package payloads with
zero placeholders. The closed plan binds each package's original URL, size and
SHA-256, the publisher unpacker and the complete reconstructed archive digest.
Build a native artifact from those verified preparation files:

```sh
python scripts/build_publisher_bootstrap.py --preparation-dir <preparation-directory> --artifact-metadata <pack>.artifact.json --output-dir <new-artifact-directory>
```

The builder emits a Linux `.sh` or Windows `.ps1` plus hash-bound managed-artifact
metadata. Its embedded compressed scaffold is capped at 64 MiB. It preserves the
original wheel, source revision and pack provenance; changed inputs, unsafe files
and existing output are rejected. Use an explicit signed release-index v3 with
the `publisher-bootstrap-executable` kind, then render the first-install wrapper
with the existing installer control. V1/v2 reject the new kind; the normal
publication workflow still defaults to v1.

The native bootstrap downloads exact publisher packages, verifies their sizes and
SHA-256 values, fills the original archive ranges and verifies the complete tar
before fetching/executing the pinned unpacker. First installation and managed
updates share this artifact path and existing profile/recovery/registration checks.
Accept the exact generated installer and installed launcher, including real
initialization, product activation and source HTTP/MCP readback. Historical preview
tests retain their own artifact scope; packaging checks alone do not renew
onboarding, engines or recovery acceptance. Final public source/notice/trust and
desktop delivery gates remain in the plan.

The Linux bootstrap requires Bash, curl, CA certificates, `flock` and the usual POSIX
file utilities. It uses the bundled Python and Pixi after verified unpacking;
neither needs an existing machine installation. The unpacker runs explicitly
under Bash, including on systems where `/bin/sh` is Dash.

The Windows bootstrap supports built-in PowerShell 5.1. Native progress on stderr
is retained, while a failed exit code still stops installation. Administrator
setup keeps its console prompts attached to the host. After verified unpacking,
the installer uses the bundled Python and Pixi rather than ambient installations.

Newly rendered entrypoints require the bundled native-ownership protocol. They
reserve the program lock before unpacking and bind each generation to its release
and resolved data root before adoption. Older payloads missing that protocol stop
before adoption. The same carrier can resume a matching ready generation with
the native resume capability; incomplete or foreign generations remain preserved.
The manager validates the original transaction under its installation leases.
See [update and recovery](update-and-recovery.md#native-ready-generation-reruns).

Native preview acceptance runs outside the development checkout. It does not
establish public release acceptance, an offline dependency closure or coverage
of every font/script. Exact artifacts, activation-time dependencies and asset
notices must be reviewed before publication.

PDF generation uses PyMuPDF Story. Documents is being prepared for AGPL-compliant
distribution with corresponding source, retaining Carlina's MIT notice.
The source and wheel licence inputs include the original browser-package texts
and their version/hash catalogue at `third_party/frontend/NOTICE.json`.
Before publication, verify the exact component/source closure, model/dependency
notices and a version-bound source offer for users of the application and MCP.
Keep the engines shared, preserve add-on generation/version identities, and
validate an exact artifact outside the development checkout. Existing preview
acceptance does not certify these public-release requirements.
