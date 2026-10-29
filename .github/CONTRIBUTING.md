# Contributing to Carlina Documents

Carlina Documents is available as an early preview, with packaged downloads
for Windows 11 x86-64 and Linux x86-64. Downloads
and their instructions appear on the
[Releases page](https://github.com/lukaszliniewicz/carlina-documents/releases).

## Set up an isolated checkout

Install Git and Pixi 0.81.0, then run `pixi install --locked` from the repository
root. The locked default environment contains the Python quality tools and Node
needed for development. Installed applications use prebuilt browser assets.

Use a dedicated development data root and unused ports. A source checkout must
never open the data root owned by an installed or running application.

Linux, from the repository root:

```bash
export CARLINA_HOME="$PWD/.workspace/development-data"
export CARLINA_PORT=5015
export CARLINA_MCP_PORT=5016
pixi run --locked setup
pixi run --locked start
```

Windows PowerShell, from the repository root:

```powershell
$env:CARLINA_HOME = Join-Path $PWD '.workspace/development-data'
$env:CARLINA_PORT = '5015'
$env:CARLINA_MCP_PORT = '5016'
pixi run --locked setup
pixi run --locked start
```

Check that the selected ports are free first. Keep these variables in the same
shell for later management commands. Configure direct model connections only
when needed, or use an authenticated MCP client for passive work. Tests should
use synthetic content and temporary roots, without real provider calls.

## Preserve the shared application contract

Read [AGENTS.md](../AGENTS.md), [architecture](../docs/architecture.md) and the
relevant component contract.
This repository owns the shared host, three document products, MCP and common UI.
Private Workbench consumes a reviewed pin and adds its own products and metadata.
Shared changes must preserve that broader composition as well as Documents.

Keep existing import compatibility, add-on IDs, migration history, root/content
policies and owner/revision/lease/idempotency checks. Never share a writable
program environment or data root between the applications. Document a proposed
boundary change before implementation; preserve unrelated and concurrent work.

## Verify the affected behavior

Run focused tests while editing, then the full affected lane and blocking gates:

```text
pixi run --locked python-quality
pixi run --locked python-audit
pixi run --locked python -m pytest -m "not uses_network" -q
pixi run --locked frontend-check
pixi run --locked frontend-lint
pixi run --locked frontend-test
```

Frontend changes also require rebuilding the affected distributions and checking
their source-derived stamps. Edit authored source; generated compositions are
not authoring locations. Broad Ruff findings remain an advisory inventory, while
strict checks, typing, import boundaries, dependency ownership and high-confidence
dead code block the audit. See [engineering policy](../docs/engineering-policy.md)
and [testing](../docs/testing.md) for exact scope and platform acceptance.

Use focused branches and coherent commits. Include the trigger, resulting
behavior, checks run and remaining limits in a pull request. Keep credentials,
private documents, runtime state, environments and operational receipts out of
Git. Preserve copyright and component notices; the [distribution decision](../docs/adr/0012-documents-agpl-distribution.md)
and [third-party policy](../THIRD_PARTY_NOTICES.md) apply to release inputs.

Report vulnerabilities through the [security policy](SECURITY.md).
