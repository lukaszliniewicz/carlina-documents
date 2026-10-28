# ADR 0011: Root-bound ordinary content policy

- Status: Accepted
- Date: 2026-10-04
- Supersedes: ADR 0003 for the ordinary-content default of fresh document roots

## Context

The document composition should run independently with simple local recovery.
Encrypting selected document fields with a key stored beside the database adds
key-management obligations without protecting source uploads, exported books or
all workspace state. Credentials need protection regardless of the content
policy. Existing installations and encrypted document prototypes must remain
readable without conversion or a change of key lineage.

## Decision

The immutable root profile marker also selects the ordinary-content codec.
Fresh Documents roots use `plaintext-v1`; fresh Workbench roots use
`installation-envelope-v2`. An existing marker keeps its recorded policy.
Markerless existing Workbench roots retain the documented legacy encrypted
compatibility path. Documents refuses markerless existing roots. Missing,
malformed or incompatible bindings never select a codec implicitly.

`ContentType` declares ordinary persisted content; the shared content codec also
handles extraction checkpoints, review history and assembly-recovery audits.
Its plaintext representation is the explicit `plain:v1:` prefix followed by the
serialized text or JSON. The encrypted representation retains the existing
authenticated `enc:v2:` envelope and installation key service. A reader accepts
only the representation selected by its root. Legacy encrypted roots retain
`sys:`, `usr:` and prefixless compatibility, except that the new `plain:`
namespace is reserved and rejected. Literal prefix text stored through either
codec round-trips normally. There is no automatic conversion of existing rows,
no ciphertext-as-text fallback and no new database schema lineage.

Ordinary fields comprise Doc Processor document paths, output and issue payloads;
Translator source/target text, structural plans, research, glossary and memory
content, and frozen workflow evidence without credentials; and MCP
execution/work-item request, response and boundary packets. Credentials remain
`EncryptedType` fields. Frozen transport identities preserve provider base URLs,
which may contain userinfo or query credentials. Translator workflow
configuration, analysis/research model records, context-artifact model identities
and evidence-reuse selections therefore retain `EncryptedType` under both
policies. Credential witness hashes and reference identifiers are not secrets.
Mixed-purpose core settings, user add-on settings, provider/plugin credentials
and runtime job payloads retain their existing protection. Changing a field's
classification requires explicit source review. This does not claim complete
encryption coverage for existing metadata or files.

Plaintext roots still require the existing persistent installation and session
keys for credentials, keyed lookup indexes and signed workflow receipts. Current
key storage is a restricted installation key file. OS keyring integration and
selectable password-locked new-record modes from ADR 0003 remain future work.
The legacy bulk encryption migration refuses plaintext roots; it cannot convert
their ordinary content behind the codec's back.

The plaintext default is enabled with the policy-aware adapters and direct
persistence paths. Storage acceptance covers both policies, restart, protected
credential copies, failure without keys and declared backup recovery. This is
source acceptance; current full-suite, native application and distribution gates
remain separate evidence.

## Consequences

- Plaintext content is readable in a database-only disclosure. It provides no
  cryptographic integrity; semantic, ownership, revision and hash checks remain.
- Authentication, owner/resource scopes, path guards and redaction apply under
  both policies. Root permissions and OS disk encryption remain useful local
  protections, without covering exported files or a compromised running session.
- Data backups must record the root marker and their actual key coverage.
  Restoring ordinary plaintext content does not restore credentials or keyed
  receipts without the original key material. Key loss is still a hard failure
  for protected fields; recovery must restore the declared keys separately.
- Existing encrypted roots stay encrypted, including the earlier Documents
  prototypes. Policy conversion is a separate, explicitly reviewed operation
  and is not implemented by this decision.
- SQLCipher and whole-workspace application encryption are deferred until a
  concrete threat model warrants their platform and recovery cost.
