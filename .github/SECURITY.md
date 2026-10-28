# Security policy

## Supported versions

Security fixes target the latest preview. Previews are experimental and have no
support guarantee, long-term support branch or promised response time.

## Report a vulnerability privately

Use this repository's [private vulnerability reporting form](https://github.com/lukaszliniewicz/carlina-documents/security/advisories/new)
when it is available. If the form is unavailable, open an issue asking for a
private reporting channel without describing the vulnerability or including
credentials, personal data, private documents or exploit details.

A private report should identify the source revision or release, operating
system, application profile, reproduction steps, expected and actual behavior,
and impact. Use a minimal synthetic example. Maintainer response and coordinated
remediation are best effort.

## Supported trust boundaries

- The supported desktop topology has one runtime owner per data root, with
  loopback HTTP and authenticated loopback MCP. Public server deployment is
  outside the first desktop release's accepted scope.
- Native add-ons are trusted code with the application's access. Process
  separation alone is not a hostile-code sandbox.
- Documents and Workbench have separate program environments and mutable roots.
  Shared source does not authorize either application to adopt the other's data.
- Fresh Documents roots store ordinary content in plaintext and protect
  credentials separately. Preserve root permissions, OS disk protection and
  recovery keys; do not describe the workspace as entirely encrypted.
- Release-index signatures, artifact hashes and profile checks protect managed
  admission. Public release-key/hosting and exact source/notices still require
  acceptance. Windows executable code signing is out of scope; downloads will
  identify that status. A checksum downloaded through the same compromised
  channel is not independent publisher verification.

See the [architecture](../docs/architecture.md),
[MCP contract](../docs/mcp-integration-architecture.md),
[recovery contract](../docs/update-and-recovery.md) and
[release plan](../docs/implementation-plan.md) for the implemented boundaries and
remaining gates. Security reports should identify a concrete violation of a
supported boundary; broader deployment proposals can be discussed separately.
