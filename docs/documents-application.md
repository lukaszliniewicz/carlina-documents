# Documents application

Use Carlina Documents to extract and review documents, translate them, and export
the results. Document Processor, Translator and Sentence Segmenter share one
browser application. You can configure a model API connection, or let an
external AI client supply the model responses through MCP.

## Current launch and first use

The [public release](https://github.com/lukaszliniewicz/carlina-documents/releases)
provides a Linux AppImage, a Windows setup EXE and script installers. The Windows
downloads are unsigned. Follow the packaged instructions below, or use the
[source setup instructions](https://github.com/lukaszliniewicz/carlina-documents/blob/main/.github/CONTRIBUTING.md#set-up-an-isolated-checkout)
to run a checkout.

### Packaged downloads

Download the AppImage for Linux or `Carlina_Documents-0.2.0a4-Setup-x86_64.exe`
for Windows. Double-click the Windows EXE and follow the setup window. It
includes Python, so you do not need to install Python separately. The installers
download the other runtime files in a release, so the first setup needs internet
access. The AppImage setup requires systemd 254 or newer, a usable systemd user
session with cgroup v2, and X11 or XWayland for its window.

On Linux, make the AppImage executable and run it. If FUSE is not available, use
the runtime's extract-and-run option:

```sh
chmod +x Carlina_Documents-0.2.0a4-x86_64.AppImage
./Carlina_Documents-0.2.0a4-x86_64.AppImage
# Fallback when mounting with FUSE is unavailable:
./Carlina_Documents-0.2.0a4-x86_64.AppImage --appimage-extract-and-run
```

Alternatively, use the shell installer:

```sh
sh ./install-carlina-documents-linux-64.sh --no-path-link
"$HOME/.local/lib/carlina-documents/carlina-documents" start --open-browser
```

For terminal setup on Windows, download `install-carlina-documents-win-64.ps1`,
open PowerShell in the download folder and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-carlina-documents-win-64.ps1 -NoPathUpdate
& "$env:LOCALAPPDATA\Programs\CarlinaDocuments\carlina-documents.ps1" start --open-browser
```

The PowerShell execution-policy option applies to that process only. These
downloads are unsigned. The script asks you to create an administrator account,
while the AppImage and the Windows EXE show a setup window for it. The commands
above use the default installation folders and skip PATH registration. Use the
installed launcher to start, stop and diagnose the application later.

After you create the account, a new installation prepares and activates Document
Processor, Translator and Sentence Segmenter. This can take several minutes
while their dependencies download. If preparation fails, reopen setup and choose
**Finish setup**. Your account and completed steps are kept. For terminal setup,
rerun the installed launcher with `init --no-admin-prompt`.

Existing installations keep their add-on choices. Sentence splitting still needs
the [model preparation step](#prepare-sentence-segmentation). The AppImage and
the Windows EXE reopen an existing installation without upgrading it. To open
Documents again, keep the downloaded setup file or use the installed launcher.
Updates and removal follow the [managed-runtime guide](update-and-recovery.md).
Menu integration and a managed uninstaller are not provided.

Source-checkout commands need a dedicated development `CARLINA_HOME` and free
HTTP and MCP ports. Do not point them at a root that an installed or running
application owns. The generated installed launcher uses its recorded managed
binding.

Use the repository's pinned Pixi version and locked `documents` environment:

```text
pixi run --locked -e documents python -m carlina.documents init
pixi run --locked -e documents python -m carlina.documents start --open-browser
pixi run --locked -e documents python -m carlina.documents status --json
pixi run --locked -e documents python -m carlina.documents doctor --json
```

The installed entry point is `carlina-documents` (the generated
`carlina-documents.ps1` launcher on Windows). Use the generated installed
launcher to initialise, start and stop the application. It supplies the managed
Pixi path and isolates user-site Python packages. It fixes the application
profile to Documents for startup, diagnostics and maintenance. Its default
listener is `http://127.0.0.1:5005`, and its loopback MCP listener starts on
port 5006. Carlina Workbench uses ports 5003 and 5004, so the two pairs of
listeners can coexist. An explicit `--port` or `CARLINA_PORT` overrides the HTTP default,
and `CARLINA_MCP_PORT` overrides the MCP default. The two applications use
separate session and remember cookies, so you can use both in one browser.

Initialisation creates the first administrator and prepares the bundled
products. They are trusted native add-ons with the same code access as the
application. Their dependency licences are listed in the
[third-party notices](../THIRD_PARTY_NOTICES.md). Older previews may need
activation through **Settings → Add-ons**. You configure model connections in
Settings. Passive MCP extraction and translation do not need a configured direct
API provider, but direct execution does.

### Prepare sentence segmentation

Block translation works without the sentence model. Sentence Segmenter and
**aligned sentence** translation need a one-time download of the pinned SaT
model files. Once setup has finished, run the following commands for your
installed copy. Change `program_root` and `$programRoot` if you chose another
folder. The commands read the cache path from that installation's status.

Linux:

```sh
program_root="$HOME/.local/lib/carlina-documents"
version=$(cat "$program_root/current")
managed_python="$program_root/versions/$version/env/bin/python"
cache_root=$("$program_root/carlina-documents" status --json | "$managed_python" -I -c 'import json,sys; print(json.load(sys.stdin)["layout"]["cache_dir"])')
"$managed_python" -I -m carlina.segmentation.prepare --cache-root "$cache_root/addons/sentence_segmenter/sat"
```

Windows PowerShell:

```powershell
$programRoot = Join-Path $env:LOCALAPPDATA 'Programs\CarlinaDocuments'
$status = (& "$programRoot\carlina-documents.ps1" status --json) | ConvertFrom-Json
$version = $status.managed_install.active_version
$modelCache = Join-Path $status.layout.cache_dir 'addons\sentence_segmenter\sat'
& "$programRoot\versions\$version\env\python.exe" -I -m carlina.segmentation.prepare --cache-root $modelCache
```

When preparation succeeds, it prints `Pinned SaT model is ready`. To verify the
files later without downloading them, add `--check`. Sentence splitting then
runs locally and needs no model API key.

For passive work, sign in as an administrator and select **Connect AI client**
on the home page. The panel shows the URL of the running listener. Give the
connection a name, and optionally authorise an existing source folder on this
computer. Without a folder, the client can work with documents that are already
imported into the account. Copy the URL and the one-time bearer token into a
client that supports MCP over HTTP. Save the token before you close the panel,
because Carlina stores only its digest. The same panel lists and revokes this
account's connections.

Each connection has document, translation and passive-work scopes. Imports must
stay inside its authorised folders. Ask the client to import and extract a small
document first, then inspect the completed revision and the boundary repairs,
and only then translate or export it. Before passive work starts, the client
declares its input and output budgets and its capabilities. It then claims
prepared packets and submits model responses. Carlina validates the responses,
keeps recovery evidence and publishes the result. A submission receipt alone
does not mean that the final document is ready.

The listener is local-only. `CARLINA_MCP_ENABLED=0` disables it at startup, and
`CARLINA_MCP_ENABLED=1` enables it explicitly. Changing this setting requires an
application restart. The full Workbench profile still starts MCP only when you
opt in.

### Optional EPUB covers

Document Processor and Translator use the same EPUB cover renderer and packager.
The export dialog defaults to the existing cover. For EPUB, an owner can instead
choose a physical PDF page or generate a static cover with the title and author.
A generated Translator cover uses the translated edition's title, subtitle,
authors and translators. A PDF cover keeps the original language of the source
page.

PDF metadata may suggest `cover_candidate: {page, kind, evidence}` or null. The
page number counts from 1 and includes unnumbered front matter. Only inspected
pages can support a suggestion, and a suggestion never changes the export
defaults. New passive runs freeze `mcp_cover_candidate_policy=pdf-page-v1`.
Retained runs without the marker keep their original schemas and prompts.

Both MCP export tools accept `epub_cover_mode` (`existing`, `pdf_page`, or
`generated`), and `epub_cover_page` for the PDF choice. Cover options are bound
to the retry fingerprint. They do not rewrite source documents, saved edition
details or translation revisions. HTML, DOCX and PDF exports keep their existing
cover behaviour.

A selected cover becomes one raster `cover-image` (PNG by default) and a
separate opening spine page, without reader-side scripting. Rendering uses owned
uploads only. It rejects password-protected, missing and out-of-range PDFs, and
limits input to 512 MiB and output to an 1800-pixel longest edge. Translator
checks the opaque HTML projection of the current owned source against its frozen
snapshot. New snapshots also freeze the PDF SHA-256. Older snapshots use the
owned PDF when the HTML witness still matches. Explicit replacement retires the
sole image resource of the original cover and preserves body text, note
identities and links.

### Revising accepted image crops

Completed PDFs offer **Adjust image crops** in the document actions menu. The
editor reopens the accepted coordinates against a retained page image, including
for images that were accepted automatically. Saving replaces only the selected
image assets in the current document, derives its Markdown and publishes the new
Knowledge version. It keeps the current text, captions and note links, and it
needs no model calls or further extraction job. Future exports use the revised
crops. Existing translation snapshots and exported files keep their earlier
image assets.

Older output may lack source coordinates. You can trim its saved image, or the
owner can select a physical PDF page and draw a replacement crop. A newly saved
crop keeps that source for later expansion and adjustment. Sources and generated
assets stay within the owned document directory. The editor submits only
coordinates, source-page choices and the document revision it requires. If the
document changed meanwhile, reload it. The initial image review keeps its own
checkpoint after extraction, with its own resume flow.

Production uses prebuilt assets. Node is needed only for development and
builds. Native document conversion and SaT segmentation use their declared,
locked managed environments. Docker, WSL, office software and LaTeX are not
requirements of this composition. Activate Sentence Segmenter before aligned
sentence source planning, and prepare its pinned model assets before offline
segmentation. Activation ignores container alternatives that are explicitly
on-demand when the host excludes container services. Required container units
still block admission. Default native workers and CPU segmentation have been
exercised on fresh minimal Linux userland and on Windows installations with a
restricted process PATH. This is accepted native preview evidence, and
whole-machine VM testing is deferred. Whole-book linguistic quality remains
separate evidence.

## Data and configuration

Documents keeps its data, configuration, cache, logs and secrets in separate
platform locations under the application name `Carlina Documents`. It does not
adopt checkout-local databases, and it does not load the checkout's `.env` or
the optional ignored Python configuration. The canonical configuration file and
an explicit process environment still work. In a source or full distribution,
the entry point, a command argument or the process environment selects the
runtime profile, and a dotenv file cannot switch it. A Documents distribution
defaults to its immutable profile. It rejects a Workbench override before it
resolves or creates a data root.

`CARLINA_HOME` selects an explicit portable root. `CARLINA_SECRETS_DIR` and
`CARLINA_ADDON_ENVIRONMENTS_DIR` keep the meanings they have in the shared
runtime. The latter allows a shorter owned worker-environment path on Windows.
The default worker path has native acceptance. Other path lengths remain
limited compatibility cases.

A root records its application profile and content policy before any durable
content is written, and a conflicting profile is refused. Fresh Documents roots
store ordinary document and translation content in plaintext and protect
credentials. Existing encrypted roots keep their policy and key lineage, and
selecting Documents does not decrypt them. See
[ADR 0011](adr/0011-root-bound-ordinary-content-policy.md). Backups keep the
profile marker. Encryption keys are a separate recovery requirement for
encrypted content and protected secrets.

## Maintainer packaging and source delivery

The source selector includes the shared runtime and only `doc_processor`,
`translator` and `sentence_segmenter`. It rejects mutable data, environments,
secrets, symlinks and unrelated add-on trees. Static assets and build inputs
form an explicit additional inventory. The builder exports a clean Git commit
and stages only those members. It adds a closed distribution descriptor that
contains the profile, the selected add-ons, the source revision and the commit
timestamp. It produces normalised wheel and source archives. The wheel also
bundles the exact normalised application source archive, with its version,
revision, byte length and SHA-256. `--wheel-only` still builds and embeds the
source. An online pack can contain only that application wheel and the
environment metadata. The dependency-source metadata wheel is optional.
Preparation and bootstrap generation reject undeclared wheels and omit absent
companion fields. From this repository's locked development environment, run:

```text
pixi run --locked python scripts/build_documents_release.py --root <clean-checkout> --revision <commit> --output-dir <artifact-directory>
pixi run --locked python scripts/verify_release_artifacts.py --application-profile documents <artifact-directory>/*.whl <artifact-directory>/*.tar.gz
```

The source archive contains the corresponding frontend source and prebuilt
assets, together with the selected recovery guide and the shared documentation
it links to. Local documentation links must resolve within that selection. The
release builder uses its generated `carlina/distribution.json` to compile only
the Documents core routes, with the pinned npm lock. It removes the temporary
npm dependencies before it packages the source and the wheel. Building a release
therefore needs Node 24 and npm 11 as well as the locked Python environment.
Installed users receive the prebuilt interface.

The Linux AppImage is an online bootstrap, not an offline application bundle.
It carries portable Python and Tk, the small setup and launch adapter, and a
generated Documents installer. That installer downloads the managed runtime and
its dependencies. Documents and installed generations live outside the AppImage.
Existing configured installations are reopened through their own launcher. The
current adapter does not upgrade an existing installation or add menu entries.
Removal and updates still follow the [managed-runtime guide](update-and-recovery.md).

Build it on Linux x86-64 from a clean, committed checkout, with the same version
as the generated installer. Supply local, hash-verified inputs. The builder does
not download tools, and it does not choose a moving runtime implicitly:

```text
pixi run --locked python scripts/build_documents_appimage.py --root <clean-checkout> --revision <commit> --version <version> --output-dir <artifact-directory> --installer <generated-installer.sh> --installer-sha256 <sha256> --index-sha256 <signed-index-sha256> --python-archive <portable-python.tar.gz> --python-sha256 <sha256> --appimagetool <appimagetool.AppImage> --tool-sha256 <sha256> --runtime <type2-runtime> --runtime-sha256 <sha256>
```

The builder uses zstd compression. It binds the copied source, the original
notices, the input hashes and the source revision to the output receipt. The
pinned bootstrap inputs and the outstanding binary and source obligations are
recorded in [the bootstrap catalogue](../third_party/appimage/NOTICE.json). Test
the actual AppImage on the declared desktop baseline. Where FUSE mounting is
unavailable, the runtime's `--appimage-extract-and-run` path needs separate
acceptance. The fact that it exists does not establish graphical or
clean-machine support. The supervised Linux installer currently requires systemd
254 or newer with a working user session, and its standard command-line
prerequisites. The Tk window uses X11 or XWayland. Declare the tested desktop
baseline explicitly. Wrapping the installer in an AppImage does not remove these
requirements.

The Windows EXE uses the same setup adapter, packaged with PyInstaller, and the
generated PowerShell installer. It downloads the same managed runtime as the
script. It runs as the current user and does not request administrator
elevation. The setup process waits for the installation to finish before its
temporary files are removed. The installed application runs from its own program
folder.

Build on x86-64 Windows with the pinned CPython 3.14.6 runtime listed in
[the Windows bootstrap catalogue](../third_party/windows-bootstrap/NOTICE.json).
Create a separate build environment and install the hash-pinned tools:

```powershell
python.exe -m venv <build-venv>
& '<build-venv>\Scripts\python.exe' -m pip install --require-hashes -r packaging/windows/requirements-build.lock
& '<build-venv>\Scripts\python.exe' scripts/build_documents_windows_installer.py --root <clean-checkout> --revision <commit> --version <version> --output-dir <artifact-directory> --installer <generated-installer.ps1> --installer-sha256 <sha256> --release-index-sha256 <signed-index-sha256>
```

The builder requires a clean committed checkout. It checks the installer's
Documents identity and hashes, and it produces the EXE, its checksum and an
artifact record. It includes the original dependency notices of the setup
source. The current release is unsigned. Test the frozen executable and the
Windows installation path on Windows. A Linux test of the adapter does not
establish Windows support.

New packaging inputs include the complete AGPL text and the distribution
decision. Older immutable previews keep their original contents. Carlina's MIT
notice covers its own source, and dependencies keep their separate terms. The
source archive and the wheel's licence directory include the exact pinned model
cards and the [asset notice mapping](../third_party/models/NOTICE.json). Explicit
preparation downloads the inference files directly from the publisher. These
notices preserve the upstream MIT declarations, because the pinned repositories
provide no standalone licence text. Updating the notices does not invalidate
prepared models. To rebuild those assets after extracting the archive, put
Node 24 and npm 11 on PATH and use the archive's locked Documents environment
from the archive root:

```text
pixi run --locked -e documents python -c "from iarf_assistant.core.frontend_orchestration import build_frontend_workspace; result = build_frontend_workspace('.', force=True); assert result['success'], result['message']"
```

This helper rebuilds the core frontend and the three selected add-on frontends,
and it writes their runtime build stamps. The archived immutable descriptor
selects the same Documents route module. A shared checkout without a descriptor
builds Workbench. Direct Vite commands alone do not regenerate those stamps.
Asset hashes can differ between build paths. Each build must keep its matching
assets, manifest and stamps together.

The separate legacy core stylesheet, `output.css`, uses the pinned standalone
Tailwind CLI. New source archives include `scripts/build_tailwind.py`. From the
archive root, rebuild the stylesheet with
`python scripts/build_tailwind.py --minify`. The builder downloads the declared
platform binary and verifies its SHA-256 before it runs the binary. Older
previews keep the generated stylesheet but omitted this builder, so they do not
establish this part of corresponding-source coverage.

To build a wheel with the archived prebuilt assets or with newly stamped assets,
run `python -m pip wheel --no-deps .` with Python 3.14. Attach the retained
archive to the resulting unpublished wheel with:

```text
python scripts/package_application_source.py --wheel <rebuilt-wheel> --source-archive <retained-source-archive>
```

This verifies that the version and revision metadata match and that the
packaged source and asset bytes match, then regenerates the wheel checksums. The
archive is self-contained: it needs no other checkout and no Git history. Changed
application code needs its own matching source artifact.

New Documents builds show **Source code** in the application shell. The public
`/api/application-distribution` projection identifies the installed version, the
revision and the verified application archive. Its hash-bound download works
without login. MCP status exposes the same identity, and authenticated clients
can read the exact bytes from `carlina://application/source/<sha256>`. A missing
or tampered bundle produces no usable source offer. Older previews still run
without this metadata. The application never falls back to serving source from
a checkout or from user data. This archive covers the application. The applicable native
dependency source and notices remain separate publication gates.

Native packs can also carry a verified, version-bound companion manifest. When
one is present, **Dependency sources** and **Third-party notices** appear
alongside **Source code**. `/api/application-distribution` and the authenticated
MCP resource `carlina://application/companions` expose download URLs, byte
lengths, SHA-256 hashes and platform inventory bindings. These are selected
companions, and discovering them does not certify complete licence or source
coverage. The app does not proxy or install the large ZIPs. If the companion
metadata is invalid, the app hides those links, and the independent
application-source download stays available.

Run the read-only inventory tool from the current shared checkout, or from a
source archive that includes it. Older preview archives are still valid inputs:

```text
python scripts/generate_documents_inventory.py --source-root <unpacked-source> --wheel <matching-documents-wheel> --platform linux-64 --output-dir <new-report-directory>
```

Use `win-64` for the Windows lock selection. The command checks the packaged
manifests, worker locks and model descriptors against the wheel, and it writes
`inventory.json` and CycloneDX 1.6 `cyclonedx.json`. It records the base,
host-vendor and native-worker contexts separately. It also records the pinned
model assets and the full frontend build lock, and the embedded application
source identity and checksum when present. It does not import application code
or download dependencies. Frontend bundle membership, installed-package notices
and corresponding-source obligations remain separate release checks, and these
reports do not certify public distribution. Pip lock hashes list the allowed
artifact alternatives. They do not prove which artifact was installed.

Collect Conda notice candidates for the same artifact's resolved inventories
with Python 3.14:

```text
python scripts/collect_documents_notices.py --inventory <linux-inventory.json> --inventory <windows-inventory.json> --cache-dir <archive-cache> --output-dir <new-notice-directory>
```

The collector downloads the exact locked Conda-forge archives over HTTPS and
verifies their SHA-256 hashes before it reads them. It supports both `.conda`
and `.tar.bz2`, including notices in payload licence directories with
nonstandard filenames. In `index.json` it keeps the unmodified notice, metadata
and recipe bytes, with their archive paths and environment references. It does
not install or execute package contents. You can reuse verified archives after
an interrupted collection, but use a new output directory. The default download
budget is 2 GiB.

The collection records missing package notice candidates as gaps and
distinguishes recipe notices from package notices. It does not accept licences,
and it does not complete the separate installed PyPI, frontend, model and
corresponding-source checks.

After you review those gaps, prepare a supplement directory with a
`manifest.json` of format `carlina.documents-conda-notice-supplement`, schema
version 1, the exact collection index SHA-256 and one decision per discovery
gap. Each decision binds an archive hash, an explanation and one of three bases:
a metadata-only wrapper, an embedded header notice or a notice from an exact
sibling package. Supplement blobs use `files/<sha256>` with their original
archive path, size and origin archive hash. Sibling notices must also match the
original bytes in the collection, and the reviewed explanation records how their
sources relate. Wrapper decisions record the operator's review of the payload.
They do not claim a new archive scan.

Assemble a portable notice directory offline:

```text
python scripts/assemble_documents_notices.py --collection-dir <verified-collection> --supplement-dir <reviewed-supplement> --output-dir <new-notice-bundle>
```

The assembler checks every retained input member, preserves the notice bytes and
requires the exact gap set. Its deterministic index keeps the package,
environment and artifact provenance, without workstation paths. It verifies the
input evidence and mappings. Licence acceptance, native source, installed PyPI,
frontend and model coverage remain separate publication checks. This
release-preparation tool requires POSIX directory-descriptor operations and has
Linux acceptance. Its output is portable. The installed application and the
Windows installer do not run the assembler.

Prepare selected dependency sources as a separate release companion:

```text
python scripts/assemble_documents_sources.py --manifest <reviewed-source-manifest.json> --files-dir <source-blobs> --inventory <linux-inventory.json> --inventory <windows-inventory.json> --output <new-source-companion.zip>
```

The closed input format is `carlina.documents-source-companion-input`, schema 1.
It binds the Documents version, revision and wheel hash to exact inventory
hashes. Component selections refer to allowed binary hashes and to named source,
recipe and notice members. File records give portable destination names, kinds,
SHA-256 values, byte lengths and the original URL, archive and member
provenance. Input blobs are regular files named by their hash, and you must
include at least one recipient instruction file. The assembler streams the
original bytes into a deterministic ZIP, rejects mismatched or unsafe inputs and
refuses an existing output. Its embedded index records the selected coverage and
keeps source closure and public acceptance false. This is a POSIX
release-preparation tool, and recipients use the portable ZIP. Release hosting,
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
records with `platform` and `sha256`. `companions` contains at most one each of
`dependency_sources` and `dependency_notices`, with `kind`, `filename`,
`byte_length`, `sha256`, `url` and `coverage: "selected"`. At least one
inventory and one download are required. URLs use HTTPS, but numeric-loopback
HTTP is allowed for local preview delivery. Credentials, query strings and
fragments are rejected. Before packing, the pack builder verifies the metadata
wheel against the exact application wheel and the target inventory. It rejects
persistent changes to its inputs before publishing. The signed pack covers the
injected metadata, and a mutable release receipt is not its trust root. Older
packs still run without companions.

The wheel keeps the canonical `carlina` package name. Install it into an
exclusive environment, because Carlina Workbench and Documents do not share a
Python prefix or an immutable program tree. Managed packs select the locked `documents`
environment, the prebuilt browser assets and the profile-specific launchers.
Signed indexes bind the profile. Bootstrap, update and rollback validate the
unpacked descriptor before they import application code. Full and legacy
packages without a descriptor keep their Workbench behaviour.

Prepare the online bootstrap inputs from a matching managed pack and platform
inventory. Build the companion metadata, the managed pack and the bootstrap from
the same source tree. For an exported Documents build, run these steps from the
exported tree, because the companion validator compares the exact bytes of the
project licence, including line endings. Source export normalises those line
endings and preserves Carlina's MIT notice and the separate AGPL text.

```text
python scripts/prepare_publisher_bootstrap.py --artifact-metadata <pack>.artifact.json --inventory <platform-inventory.json> --output-dir <new-preparation-directory>
```

The command verifies the original pack and produces `plan.json`, a deterministic
`skeleton.tar.gz` and `preparation.json`. The skeleton keeps the application
wheels and the environment metadata, and replaces the publisher package payloads
with zero placeholders. The closed plan binds each package's original URL, size
and SHA-256, the publisher unpacker and the digest of the complete reconstructed
archive. Build a native artifact from those verified preparation files:

```sh
python scripts/build_publisher_bootstrap.py --preparation-dir <preparation-directory> --artifact-metadata <pack>.artifact.json --output-dir <new-artifact-directory>
```

The builder emits a Linux `.sh` or a Windows `.ps1`, plus hash-bound
managed-artifact metadata. Its embedded compressed scaffold is capped at 64 MiB.
It preserves the original wheel, source revision and pack provenance, and it
rejects changed inputs, unsafe files and existing output. Use an explicit signed
release-index v3 with the `publisher-bootstrap-executable` kind, then render the
first-install wrapper with the existing installer control. V1 and v2 reject the
new kind, and the normal publication workflow still defaults to v1.

The native bootstrap downloads the exact publisher packages and verifies their
sizes and SHA-256 values. It fills the original archive ranges and verifies the
complete tar before it fetches and runs the pinned unpacker. First installation
and managed updates share this artifact path and the existing profile, recovery
and registration checks. Accept the exact generated installer and the installed
launcher, including real initialisation, product activation and a readback of
the source over HTTP and MCP. Historical preview tests keep their own artifact
scope. Packaging checks alone do not renew the onboarding, engine or recovery
acceptance.

The Linux bootstrap requires Bash, curl, CA certificates, `flock` and the usual
POSIX file utilities. After verified unpacking it uses the bundled Python and
Pixi, and neither needs an existing machine installation. The unpacker runs
explicitly under Bash, including on systems where `/bin/sh` is Dash.

The Windows bootstrap supports the built-in PowerShell 5.1. It keeps native
progress on stderr, while a failed exit code still stops the installation.
Administrator setup keeps its console prompts attached to the host. After
verified unpacking, the installer uses the bundled Python and Pixi, not ambient
installations.

Newly rendered entrypoints require the bundled native-ownership protocol. They
reserve the program lock before unpacking, and they bind each generation to its
release and resolved data root before adoption. Older payloads without that
protocol stop before adoption. The same carrier can resume a matching ready
generation with the native resume capability. Incomplete or foreign generations
are preserved. The manager validates the original transaction under its
installation leases. See
[update and recovery](update-and-recovery.md#native-ready-generation-reruns).

Native preview acceptance runs outside the development checkout. It does not
establish public release acceptance, an offline dependency closure or coverage
of every font and script. Review the exact artifacts, the activation-time
dependencies and the asset notices before publication.

PDF generation uses PyMuPDF Story. Documents is being prepared for
AGPL-compliant distribution with corresponding source, and keeps Carlina's MIT
notice. The licence inputs of the source and the wheel include the original
browser-package texts and their version and hash catalogue at
`third_party/frontend/NOTICE.json`. Before publication, verify the exact
component and source closure, the model and dependency notices, and a
version-bound source offer for users of the application and of MCP. Keep the
engines shared, preserve the add-on generation and version identities, and
validate an exact artifact outside the development checkout. Existing preview
acceptance does not certify these public-release requirements.
