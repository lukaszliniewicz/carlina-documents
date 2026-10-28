# Documents repository agreement

Read docs/architecture.md, docs/implementation-plan.md and docs/project-ledger.md
before substantial changes. Source, contracts, tests and release receipts override
prose. The parent owns architecture, integration, acceptance and all UI work;
specialists gather bounded non-UI evidence or implement an exact named contract.
Announce specialist profiles/models, preserve concurrent work and do not create a
second planner.

This repository is the single source for the shared Carlina runtime, Document
Processor, Translator, Sentence Segmenter, MCP implementation and shared UI.
Private Carlina consumes pinned source; changes to shared modules happen here.
Application compositions may own distinct configuration and generated UI assets.
Preserve carlina/iarf_assistant imports, add-on identifiers, migrations, root
binding, content/key policies, owner/revision/lease/idempotency fences and recovery
evidence. Repository movement never implicitly converts user data.

Use Git status/HEAD/worktrees before edits; rg for search and apply_patch for
manual changes. Commit coherent reviewed changes. Never publish or change repo
visibility without user authorization. Do not include user data, secrets, private
operational receipts, environments, caches or private repository history.

Run pixi run python-quality and python-audit. Legacy debt remains an explicit
bounded baseline; new code is strict. Run focused tests, then the full affected
offline lane. Keep import-boundary, dependency ownership, dead-code and typing
checks. For frontend work run npm run check, npm run lint and npm test; rebuild
affected prebuilt assets and verify their stamps. Linux is not Windows acceptance.

Use isolated development data roots. Resolve process, program and data ownership
before mutations; never point a source process at a managed live root. No source
task authorizes deployment, provider writes, migrations of real data or deletion
of recovery anchors. Preserve copyright/licence notices and version-bound source
delivery. Documentation describes stable contracts; private acceptance evidence
stays in the private project.
