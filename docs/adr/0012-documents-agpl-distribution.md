# ADR 0012: Documents AGPL distribution

- Status: Accepted
- Date: 2026-10-06

## Context

Documents uses PyMuPDF for native document processing and PDF generation through
Story. The native Windows and Linux previews have exercised this renderer.
PyMuPDF offers AGPL or commercial terms; separating the application from the
private repository does not remove the distribution obligations. Replacing the
renderer would reopen layout, font and native acceptance work.

## Decision

Keep PyMuPDF and prepare the public Documents application for distribution in
compliance with AGPL-3.0. The operator has accepted this direction. It does not
authorize publication or establish that an existing preview is compliant.

Carlina's own source retains its [MIT license](../../LICENSE). Original
copyright, license and attribution notices remain intact, including dependencies'
separate terms. The Python project's MIT metadata describes Carlina's source;
it does not describe every component or the assembled application as MIT-only.
Ship the complete, unmodified [AGPL text](../../COPYING-AGPL-3.0.txt) with
Documents' wheel and corresponding source. Review the remaining components under
the [third-party policy](../../THIRD_PARTY_NOTICES.md).

Each public binary release must have corresponding source for that exact release,
with its revision, locks, manifests and required build, install and execution
controls. This includes the applicable source and notices of distributed
copyleft components, rather than assuming Carlina's source archive covers native
libraries. Preserve independently licensed font/model notices and any required
modification records. An inventory or upstream URL alone is not acceptance of
these obligations.

The public application must provide a prominent, version-bound way to obtain its
corresponding source at no charge. Cover the browser application and MCP
discovery, including applicable AGPL section 13 obligations for remote
interaction. The source location must refer to the running release and actually
serve its retained source; do not add a placeholder public URL. Source delivery,
notices and their native installed-artifact coverage remain release gates.

Use the allowlisted Documents source composition. Private content, credentials,
operational receipts, unrelated add-ons and monorepo history stay outside public
artifacts. Publishing a complete required source closure must not depend on
access to the private project. Existing immutable preview artifacts retain their
identity and evidence; new packaging controls do not retrofit them.

## Consequences

- PyMuPDF Story and the accepted native extraction/rasterisation path remain the
  baseline; a renderer replacement is unnecessary for this release direction.
- Distribution is prepared as an open-source application with the applicable
  AGPL obligations. Original MIT notices remain, and dependencies keep their
  own terms. Commercial licensing is not the chosen Documents release path.
- Exact component notices, required source coverage, source availability and
  installed Windows/Linux artifact verification must pass before publication.
- The [Documents guide](../documents-application.md) remains the user/build
  entry point. This decision does not create a public release channel or claim
  that the current source, binary or network source offer is already accepted.
