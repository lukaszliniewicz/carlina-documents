# Third-party notices

Carlina's own source is licensed under the MIT License. Third-party software,
fonts and model assets retain their own terms. Public release acceptance requires
an exact inventory generated from the locked environments, including downloaded
add-on dependencies and model assets. The chosen Documents online carrier has
artifact-bound application source and original-notice acceptance; its pinned
publisher downloads are separate from bundled membership. Public delivery and
any applicable component obligations still require release review. Frozen offline
previews retain unresolved native redistribution/source coverage. This file
identifies distribution decisions and does not replace upstream license texts
or certify an individual artifact.

## Workbench release profile

Carlina's Workbench host wheel, managed runtime, and container profile do not install
PyMuPDF, PyMuPDF4LLM, extract-msg, pcodedmp, RTFDE, or DiskCache. Core PDF
inspection, embedded-text extraction, and splitting use `pypdf`, which reports
a BSD-3-Clause license. Raster previews and vision-model image conversion use
`pypdfium2`, whose distribution reports BSD-3-Clause, Apache-2.0, and bundled
dependency licenses. Add-on activation can install separately locked workers and
host-vendor dependencies; those are part of the distribution review too.

DiskCache was removed after
[`PYSEC-2026-2447`](https://osv.dev/vulnerability/PYSEC-2026-2447) documented
unsafe default pickle deserialization through 5.6.3. Runtime queues and shared
rate limits now use Carlina's JSON-only SQLite store and never import legacy
DiskCache queue records.

[`policy/release-exceptions.json`](policy/release-exceptions.json) contains no
current public-release waivers. A new advisory or license exception must still
be owned, time-bounded, and absent from a public release when marked blocking.

## Document-format dependencies

Activating Document Processor in Documents or Workbench installs PyMuPDF for PDF
generation and native document processing. Its locked host-vendor and native
worker distributions both belong in release acceptance; neither is installed by
the base host package alone. Documents retains PyMuPDF and follows the accepted
[AGPL distribution decision](docs/adr/0012-documents-agpl-distribution.md).
Carlina's own MIT notices remain. New packaging inputs ship the complete
[AGPL text](COPYING-AGPL-3.0.txt); exact corresponding-source, notice and source-offer
acceptance remain public-release gates. Existing previews retain their original
contents and are not certified by this decision.

The following packages are not part of the Workbench default release runtime. An
administrator may choose to download them by activating Document Processor or
a corresponding PDF provider, or a source-install user may deliberately select
the `licensed-document-formats` extra. Carlina shows these notices and requires
explicit acknowledgement before add-on activation. The operator/distributor
must review the actual upstream terms and decide whether to comply with the
open-source license or obtain commercial terms where offered.

| Package | Upstream-reported license family | Optional purpose |
|---|---|---|
| PyMuPDF / PyMuPDF4LLM | GNU AGPL-3.0 or commercial terms | PDF rendering, layout extraction, previews, page operations, and the PyMuPDF provider |
| extract-msg | GNU GPL-3.0 | Outlook MSG parsing in Document Processor |
| pcodedmp | GNU GPL-3.0 family | Transitive dependency used by extract-msg |
| RTFDE | GNU LGPL-3.0 family | Transitive rich-text body decoding used by extract-msg |

Primary upstream references:

- [pypdf BSD-3-Clause license](https://github.com/py-pdf/pypdf/blob/main/LICENSE)
- [pypdfium2 licensing and notices](https://github.com/pypdfium2-team/pypdfium2/tree/main/LICENSES)
- [PyMuPDF licensing documentation](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)
- [PyMuPDF4LLM licensing FAQ](https://pymupdf.readthedocs.io/en/latest/faq/index.html#pymupdf4llm-ai-pipelines)
- [extract-msg GPL-3.0 license](https://github.com/TeamMsgExtractor/msg-extractor/blob/master/LICENSE.txt)
- [pcodedmp GPL-3.0 license](https://github.com/bontchev/pcodedmp/blob/master/LICENSE)
- [RTFDE project metadata](https://pypi.org/project/RTFDE/)

This notice records the technical distribution boundary; it is not legal
advice or a substitute for reviewing the complete license texts.

## Native document and segmentation tools

The native document profile includes Pandoc and prepares add-on environments from
their packaged locks. Online installers download native packages directly from
their pinned publishers; bundled previews retain their original package contents.
SaT model/tokenizer assets are fetched by the explicit preparation command. A
host-only package inventory therefore cannot establish the complete installed
dependency or notice set.

| Component | Upstream-reported terms | Purpose |
|---|---|---|
| Pandoc | GPL-2.0-or-later | EPUB/DOCX conversion; invoked as an executable |
| wtpsplit-lite | MIT | Native SaT segmentation engine |
| Hugging Face tokenizers 0.23.1 / 0.21.1 | Apache-2.0 | Base / native-worker tokenizer execution |
| FlatBuffers 25.12.19 | Apache-2.0 | Native ONNX runtime's Python dependency |
| SaT model `segment-any-text/sat-12l-sm` | MIT in the pinned model card | Sentence-boundary model |
| XLM-R `FacebookAI/xlm-roberta-base` tokenizer assets | MIT in the pinned model card | Tokenizer data, separate from the tokenizers package |

The asset revisions and hashes are fixed in `carlina/segmentation/sat_assets.py`;
package versions are fixed in the owning Pixi locks. The unmodified pinned model
cards and their hash-bound asset mapping are retained in
[`third_party/models/NOTICE.json`](third_party/models/NOTICE.json). Packaging
includes these notices in both the source archive and the wheel's licence
directory. Neither pinned repository supplies a standalone licence file; the
cards preserve the publisher's MIT declaration without inventing a copyright
notice or licence grant. Model-card metadata is not a substitute for any required
licence text. Explicit preparation downloads the three pinned inference files
directly from the publisher; model weights and tokenizer data are not bundled in
the application wheel.
Graphviz renders semantic diagrams during document/translation export and is
part of the native runtime closure. Fonts and native libraries included by Conda
packages also require an exact package/license inventory; PDF embedding must be reviewed against the
actual fonts used.

Document Processor's optional JPEG export choice uses `pyjpegli` 0.3.2, a
third-party Python binding to Google's Jpegli encoder. The add-on's host-vendor
and Pixi locks own this dependency; the base host package does not install it.
The binding is BSD-3-Clause and its wheels retain the bundled Jpegli, Highway,
skcms and libjpeg-turbo notices in their distribution licence directory. Preserve
those original notices when materializing or distributing the dependency.
See the [pinned package](https://pypi.org/project/pyjpegli/0.3.2/) and
[Jpegli source](https://github.com/google/jpegli).

Documents elects the FreeType License (FTL) option for its locked FreeType
libraries. This software is based in part on the work of the FreeType Team.
Retain the unmodified `FTL.TXT` and any separately applicable notices in the
distribution. Library packages that obtain notices from a sibling package must
have the same exact recipe source URL and SHA-256; their release notice mapping
records both package identities. This choice does not change the terms of files
that carry their own explicit licence.

Documents elects Apache-2.0 for the bundled `self_cell` 1.2.2 and 1.3.0
declarations, and MIT for `r-efi` 5.2.0, 5.3.0 and 6.0.0. These exact versions
offer those alternatives in their original Cargo metadata and owner-bound SBOMs.
Retain each `self_cell` archive's original `LICENSE-APACHE` and any separately
applicable notices. For `r-efi`, the original `AUTHORS` file contains the MIT grant
and copyright identities; its README expressly points there for licence details.
Preserve the full unmodified file. These choices concern those crate versions,
not every component embedded by their Python owners. Release-specific mappings
and original notice bytes must accompany the distribution.

Primary upstream references:

- [FreeType licence options and attribution](https://freetype.org/license.html)
- [Pandoc copyright and license](https://pandoc.org/MANUAL.html#authors)
- [wtpsplit-lite 0.2.0 MIT license](https://github.com/superlinear-ai/wtpsplit-lite/blob/v0.2.0/LICENSE)
- [tokenizers 0.23.1 Apache license](https://github.com/huggingface/tokenizers/blob/v0.23.1/LICENSE)
- [tokenizers 0.21.1 Apache license](https://github.com/huggingface/tokenizers/blob/133db48d4f272070076fbc7a649c9e8aa8cd0646/LICENSE)
- [FlatBuffers 25.12.19 Apache license](https://github.com/google/flatbuffers/blob/7e163021e59cca4f8e1e35a7c828b5c6b7915953/LICENSE)
- [Pinned SaT model card](https://huggingface.co/segment-any-text/sat-12l-sm/blob/d70c72a9331b2d5a9e82baad00c64964a23a09bb/README.md)
- [Pinned XLM-R model card](https://huggingface.co/FacebookAI/xlm-roberta-base/blob/e73636d4f797dec63c3081bb6ed5c7b0bb3f2089/README.md)

## Frontend license choice

The Workbench frontend includes Svelte, clsx, esm-env, SvelteFlow and its D3
dependencies, plus ELKJS in the separate layout worker. The Documents build
selects its smaller route module before compilation, omitting the workflow editor
and layout worker. The shared notice catalogue includes originals for both build
profiles; each release must verify its actual emitted membership. Selected packages and
their notices follow emitted bundle membership and generated helper ownership.
The original publisher licence files and their version/hash mapping are retained
in [`third_party/frontend/NOTICE.json`](third_party/frontend/NOTICE.json).
Packaging includes that catalogue and the original texts in the source archive
and wheel's licence directory. This selected catalogue does not establish complete
coverage of dependencies compiled inside ELKJS or other publisher artifacts.
Tailwind 4.3.3 generates the current Vite styles; the retained legacy core stylesheet
uses the separately pinned standalone Tailwind 4.3.0 builder. Both are subject
to their upstream MIT terms, and the latter's version does not come from the
npm lock.

Primary upstream references:

- [Tailwind 4.3.3 license](https://github.com/tailwindlabs/tailwindcss/blob/v4.3.3/LICENSE)
- [Standalone Tailwind 4.3.0 license](https://github.com/tailwindlabs/tailwindcss/blob/v4.3.0/LICENSE)

The focused Documents frontend omits ELKJS, SvelteFlow and D3. The original
shared catalogue still retains their notices for Workbench and historical previews.

For Workbench and shared-core previews, `elkjs` offers an EPL-2.0 or GPL alternative. Carlina elects the EPL-2.0 option
for distribution. A release bundle must include the applicable EPL notice and
the exact version in its generated dependency inventory.

## Release process

The Linux AppImage bootstrap uses portable CPython/Tk and the AppImage type-2
runtime. Its pinned inputs and original upstream notice catalogue are retained
in [the AppImage notice inventory](third_party/appimage/NOTICE.json). The builder
copies those texts into the AppImage. The upstream Python install-only archive
omits the source repository's third-party licence directory; retaining its
installed CPython licence alone is insufficient for that catalogue. Some texts
cover other upstream build targets, so this collection is not a claim about
minimal binary membership or completed corresponding-source acceptance.
`appimagetool` is a build tool, separate from the shipped runtime.

The Windows setup EXE bundles CPython/Tk and uses PyInstaller's bootloader and
runtime hooks. Its pinned runtime, build tools and original licence texts are
listed in [the Windows bootstrap inventory](third_party/windows-bootstrap/NOTICE.json)
and copied into the EXE. PyInstaller's bootloader exception permits distributing
generated applications under their own licence; its own notices remain included.
The application runtime downloaded by setup retains its separate dependency
inventory and source companions.

For a public binary candidate, use the existing inventory tooling to generate
and retain:

- a CycloneDX JSON SBOM covering resolved host and activated-worker dependencies;
- machine-readable Python, npm, Conda/native-tool, font and model-asset inventories;
- the source commit, lock files, artifact manifests, and tool versions;
- all license texts and notices required by the selected dependency licenses.

A source-only repository preview preserves its original notices, source licence
and assembled-application distribution statement. Qualification of unshipped
binary payloads does not gate that milestone. Each later binary release needs
the inventory and required component-source coverage for the artifacts it
actually distributes.

For Documents, retain and deliver the required corresponding source for the
exact release, including build/install controls and applicable native components.
Verify a prominent, version-bound source offer in the application and MCP
discovery. The public source must be obtainable without access to the private
repository or its history.

Include package-supplied SBOMs and notices for bundled compiled components;
top-level Python package names do not establish their complete coverage. Installed
wheels may carry these in their
[`dist-info/sboms` directory](https://packaging.python.org/en/latest/specifications/recording-installed-packages/#the-sboms-subdirectory).
Preserve those reports with the exact package bytes and review their component
identities and notice obligations before accepting the release inventory.

`scripts/generate_documents_inventory.py` produces a resolved-lock inventory and
CycloneDX 1.6 report for a matching Documents source/wheel pair and platform.
Its coverage explicitly remains incomplete until installed notice collection,
frontend bundle membership and distribution decisions are reviewed. See the
[Documents guide](docs/documents-application.md#packaging-boundary).

Dependencies whose metadata is missing, ambiguous, copyleft, source-available,
or commercially restricted require a documented distribution decision. Any
exception must have an owner, reason, remediation and expiry. An exception marked
`release_blocking` cannot be used to publish a public binary artifact.
