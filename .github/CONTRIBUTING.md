# Contributing to Carlina Documents

Carlina Documents is an early preview. Packaged downloads exist for Windows 11
x86-64 and Linux x86-64. You can find them, with instructions, on the
[Releases page](https://github.com/lukaszliniewicz/carlina-documents/releases).

## Set up an isolated checkout

Install Git and Pixi 0.81.0, then run `pixi install --locked` from the
repository root. The locked default environment contains the Python quality
tools and the Node tooling that development needs. Installed applications use
prebuilt browser assets.

Use a dedicated development data root and ports that are not in use. A source
checkout must never open a data root that an installed or running application
owns.

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

Check first that the ports you chose are free. Keep these variables set in the
same shell for later management commands. Configure direct model connections
only when you need them, or use an authenticated MCP client for passive work.
Tests should use synthetic content and temporary roots, and should not make real
provider calls.

## Preserve the shared application contract

Read the [architecture](../docs/architecture.md) and the relevant component
contract.
This repository owns the shared host, the three document products, MCP and the
common UI. The private Workbench application uses a reviewed pin of this code
and adds its own products and metadata. A shared change must keep working in
that wider setup as well as in Documents.

Keep import compatibility, add-on IDs, migration history, root and content
policies, and the owner, revision, lease and idempotency checks. Never share a
writable program environment or data root between the applications. Document a
proposed boundary change before you implement it, and preserve unrelated and
concurrent work.

## Verify the affected behavior

Run focused tests while you edit, then the full affected lane and the blocking
gates:

```text
pixi run --locked python-quality
pixi run --locked python-audit
pixi run --locked python -m pytest -m "not uses_network" -q
pixi run --locked frontend-check
pixi run --locked frontend-lint
pixi run --locked frontend-test
```

For frontend changes, also rebuild the affected distributions and check their
source-derived stamps. Edit the authored source, not the generated compositions.
Broad Ruff findings are an advisory inventory only. The audit fails on strict
checks, typing, import boundaries, dependency ownership and high-confidence dead
code. See [engineering policy](../docs/engineering-policy.md)
and [testing](../docs/testing.md) for exact scope and platform acceptance.

Use focused branches and coherent commits. In a pull request, describe what
triggered the change, the behaviour that results, the checks you ran and any
remaining limits. Keep credentials, private documents, runtime state,
environments and operational receipts out of Git. Preserve copyright and
component notices. The [distribution decision](../docs/adr/0012-documents-agpl-distribution.md)
and the [third-party policy](../THIRD_PARTY_NOTICES.md) apply to release inputs.

Report vulnerabilities through the [security policy](SECURITY.md).
