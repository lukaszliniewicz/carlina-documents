# Security policy

## Supported versions

Security fixes target the latest preview. Previews are experimental. They come
with no support guarantee, no long-term support branch and no promised response
time.

## Report a vulnerability privately

When it is available, use this repository's [private vulnerability reporting form](https://github.com/lukaszliniewicz/carlina-documents/security/advisories/new).
If the form is unavailable, open an issue that asks for a private reporting
channel. Do not describe the vulnerability in that issue, and do not include
credentials, personal data, private documents or exploit details.

A private report should state the source revision or release, the operating
system, the application profile, the steps to reproduce, the expected and actual
behaviour, and the impact. Use a minimal synthetic example. The maintainers
respond and coordinate remediation on a best-effort basis.

## Supported trust boundaries

- The supported desktop setup has one runtime owner per data root, with
  loopback HTTP and authenticated loopback MCP. Public server deployment is
  outside the accepted scope of the first desktop release.
- Native add-ons are trusted code with the same access as the application.
  Process separation alone is not a sandbox against hostile code.
- Documents and Workbench have separate program environments and separate
  mutable roots. Sharing source code does not allow either application to adopt
  the other's data.
- Fresh Documents roots store ordinary content in plaintext and protect
  credentials separately. Keep the root's permissions, your operating system's
  disk protection and your recovery keys safe. Do not describe the workspace as
  entirely encrypted.
- Release-index signatures, artifact hashes and profile checks protect managed
  admission. The public release key and hosting, and the exact source and
  notices, still need acceptance. Windows executable code signing is out of
  scope, and downloads will say so. A checksum downloaded through the same
  compromised channel is not independent verification of the publisher.

The [architecture](../docs/architecture.md),
[MCP contract](../docs/mcp-integration-architecture.md) and
[recovery contract](../docs/update-and-recovery.md) describe the implemented
boundaries. A security report should identify a concrete violation of a
supported boundary. You can discuss broader deployment proposals separately.
