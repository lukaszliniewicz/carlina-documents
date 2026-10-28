# Carlina Documents

Carlina Documents helps you extract, review and translate documents on your own
computer, then turn the results into EPUB, DOCX, HTML or PDF.

## What you can do

- **Extract and review documents.** Import PDF, EPUB or DOCX files. Review text,
  headings, tables, images and linked notes before publishing.
- **Translate with the source alongside.** Keep source and translation aligned,
  maintain a glossary, and review changes before exporting.
- **Prepare and publish text.** Split text into sentences locally, edit edition
  details and covers, and export in several formats.
- **Work with your choice of AI.** Configure a model API in Carlina, or connect an
  AI client through MCP and let that client handle the model responses. The MCP
  option does not require a separate model API key in Carlina.

The three tools—Document Processor, Translator and Sentence Segmenter—share one
browser interface. The application runs locally; your chosen AI provider may
process document content remotely. Local sentence segmentation does not need a
model API.

## Download

**Version 0.2.0a1 is an early source preview.** You can try it from a source
checkout using the steps below. Ready-to-run downloads are still being prepared
and will appear under **Assets** on the
[Releases page](https://github.com/lukaszliniewicz/carlina-documents/releases).

Planned downloads include a Linux x86-64 AppImage and installers for Linux x86-64
and Windows 11 x86-64. Initial setup will need an internet connection to download
runtime packages and model files. Windows downloads will be unsigned.

Expect rough edges in this preview, including manual activation of the document
tools. Feedback and reproducible bug reports are welcome in
[Issues](https://github.com/lukaszliniewicz/carlina-documents/issues).

## Get started

Install Git and [Pixi 0.81.0](https://github.com/prefix-dev/pixi/releases/tag/v0.81.0)
first. On Linux:

```bash
git clone https://github.com/lukaszliniewicz/carlina-documents.git
cd carlina-documents
export CARLINA_HOME="$PWD/.workspace/development-data"
export CARLINA_PORT=5015
export CARLINA_MCP_PORT=5016
pixi run --locked setup
pixi run --locked start
```

Setup creates your administrator account; start opens the application in your
browser. Keep the same terminal open for later commands, and use different ports
if these are already taken. The dedicated data folder keeps this checkout
separate from any existing installation.

In **Settings → Add-ons**, activate Document Processor, Translator and Sentence
Segmenter. Then configure a model connection in Settings, or choose **Connect AI
client** on the home page to use MCP. Some tools download additional models on
first use.

See the [setup guide](.github/CONTRIBUTING.md#set-up-an-isolated-checkout) for
Windows PowerShell commands and development checks, and the
[Documents guide](docs/documents-application.md) for connections, first use and
exports.

Documents are stored locally in plaintext by default; credentials are protected
separately. Keep backups and protect your disk as you would for other private
documents. See the [security policy](.github/SECURITY.md) for details.

## Documentation and development

The [documentation index](docs/README.md) links to user guides and technical
references. To contribute, start with the [contributor guide](.github/CONTRIBUTING.md).
The [release plan](docs/implementation-plan.md) describes upcoming work.

Please report sensitive security issues through
[private vulnerability reporting](.github/SECURITY.md).

## Licence

Carlina's own code is [MIT licensed](LICENSE). The complete Documents application
includes PyMuPDF and is distributed under the
[AGPL terms described here](docs/adr/0012-documents-agpl-distribution.md).
Other dependencies retain their own licences; see the
[third-party notices](THIRD_PARTY_NOTICES.md) and [AGPL text](COPYING-AGPL-3.0.txt).
