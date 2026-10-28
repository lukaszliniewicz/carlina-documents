# Carlina Documents

Extract, review and translate PDF, EPUB and DOCX documents on your own computer.
Keep headings, tables, lists, linked notes and page references, then export EPUB,
DOCX, HTML or PDF.

- **Document Processor** extracts PDF content and imports EPUB/DOCX for review.
- **Translator** manages aligned translation, terminology and revision-bound
  review, with the same publishing tools as the extractor.
- **Sentence Segmenter** prepares text locally using native SaT models.

Use your own model API connection, or connect an external AI client through
authenticated MCP and let it supply model responses. Passive MCP work does not
require a direct API credential in Carlina.

## Project status

**Source preview, version 0.2.0a1.** Start with a source checkout using the
instructions below. Packaged downloads are being prepared. Native Windows/Linux
script installers and matching application-source delivery have been exercised;
the Linux AppImage has passed an existing-host graphical installation trial.
Desktop integration and guided product preparation remain in progress. The
[release plan](docs/implementation-plan.md) separates the source preview, native
installer preview and desktop beta.

The release targets are Windows 11 x86-64 and Linux x86-64. The installed runtime
uses prebuilt browser assets and native workers; it needs no WSL, Docker or office
suite. Initial setup downloads runtime packages and model assets. Direct APIs
need network access; passive work needs a compatible external MCP client.
Windows installers will be unsigned.

## Get started

For a source checkout, install Git and Pixi 0.81.0 and follow the
[contributor setup](.github/CONTRIBUTING.md). It includes Linux and PowerShell
commands, an isolated development data root, ports and verification tasks.

The [Documents guide](docs/documents-application.md) explains launch, product
activation, model connections, passive MCP and exports. Fresh Documents roots
store ordinary document content in plaintext and protect credentials separately.
Use appropriate disk protection and keep recovery keys safe. See the
[security policy](.github/SECURITY.md) for the supported local topology.

## Documentation and development

Start with the [documentation index](docs/README.md),
[architecture](docs/architecture.md) and [current status](docs/project-ledger.md).
The [contributor guide](.github/CONTRIBUTING.md) covers checks and pull requests;
report vulnerabilities through [security reporting](.github/SECURITY.md).

This repository also owns the runtime and authored UI shared with the private
Carlina Workbench. Workbench consumes a reviewed source pin, adds its own products
and keeps separate program/data roots. Changes here must preserve that consumer;
its private add-ons and operational records are outside this project.

## Licence

Carlina's own source retains its [MIT licence](LICENSE). The assembled Documents
application uses PyMuPDF and follows the
[AGPL distribution decision](docs/adr/0012-documents-agpl-distribution.md).
Dependencies retain their separate terms; see
[third-party notices](THIRD_PARTY_NOTICES.md) and the
[AGPL text](COPYING-AGPL-3.0.txt). Binary releases require matching source and
component notices; the application is not described as MIT-only.
