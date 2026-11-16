# Updates, backups, and recovery

Carlina separates rollback of immutable code from recovery of mutable data. The
managed updater installs program generations side by side, and the add-on
installer selects immutable package generations independently of core.

## Current behavior versus accepted target

Published release indexes carry no authenticated impact declaration. Managed core
updates therefore remain objectively `opaque`, and the automatic policy creates a
paired complete and overlay recovery set before the target runs. An operator can
explicitly select a whole-SQLite checkpoint or no mutable-data recovery. In those
modes, preflight does not size, scan, hash or copy uploads. Updates to installed
add-on code and UI already avoid the core path, but program-tree packages still
ship as core and so inherit its impact classification.

The [package boundary](architecture.md) retains two separate transitions:

- every native add-on, including the required and default bootstrap packages,
  moves to independently selected immutable package generations; and
- managed transactions separate objective impact from operator-selected recovery
  coverage before any payload traversal. This is implemented for managed core
  releases. The transfer of package ownership remains planned.

## Impact and coverage

Impact answers **what the operation may affect**:

| Impact | Meaning |
|---|---|
| `none` | Only immutable code and its selection pointer may change |
| `reversible_metadata` | Only exact journaled ownership and selection metadata may change |
| `migration` | A constrained host-executed data-only migration may run |
| `opaque` | Arbitrary hooks, files, external effects, or incomplete knowledge may exist |

The operator cannot override impact. Missing, legacy, unsigned or inconsistent
evidence becomes `opaque`. Existing Python and Alembic migrations and arbitrary
lifecycle hooks remain opaque, because the language can touch more than SQLite.

Coverage answers **what recovery material to create**:

| Selection | Material |
|---|---|
| `automatic` | Use the default derived from authenticated impact |
| `full` | Complete verified application recovery set |
| `database_only` | Consistent verified whole-SQLite checkpoint |
| `none` | No mutable-data recovery material |

The full file inventory includes `doc_processor_exports` and `translator_exports`
under the application instance root. These private downloads are durable results
referenced by completed jobs and operations. Both complete and overlay archives
retain their bytes and hashes. Backup refuses symbolic links and reparse points
in owned storage. Older archives that omitted these categories remain incomplete
for export recovery, and a newer controller cannot reconstruct their missing
files.

Automatic mapping:

| Impact | Automatic coverage |
|---|---|
| `none` | Program or package pointer rollback; no data archive |
| `reversible_metadata` | Exact metadata journal |
| constrained `migration` | Database-only checkpoint |
| `opaque` | Full recovery |

An operator can explicitly choose broader or narrower coverage in development, in
a managed installation or in production. A narrower choice does not make the
operation less opaque. `opaque + database_only` can restore SQLite but may leave
owned-file and external effects unreversed. `opaque + none` accepts every
mutable-data gap.

Preflight and receipts record:

- the declared and effective impact;
- the automatic default and the selected coverage;
- the actor, and the reason or policy;
- whether the origin was interactive or unattended;
- the identity of the recent receipt-bound full recovery; and
- the uncovered database, filesystem and external domains.

Carlina needs a constrained migration mechanism before it can automatically claim
that database-only coverage suffices. An operator can honestly choose partial
coverage without one.

## Common operations

| Operation | Normal protection after ADR 0010 implementation |
|---|---|
| Source edit, test and commit | Git; no live recovery material |
| Frontend rebuild | Git and a reproducible bundle; no live recovery material |
| Installed add-on code or UI update | New immutable generation and previous marker |
| Disable or remove add-on code | Lifecycle journal; owned data retained |
| Proven no-data core update | New program generation and previous pointer |
| Opaque migration or hook | Full by default; explicit database-only or none allowed |
| Purge, machine move or periodic anchor | Full by default; explicit override remains operator policy |

Do not use backup creation as a substitute for Git, deterministic packages, or
add-on modularity.

## Managed core update

### Installation event interface

The Python `install`, applying `update` and `rollback` commands accept
`--events-jsonl`. This opt-in interface writes one compact version-1 JSON event
per stdout line. Ordinary maintainer output and `--json` keep their existing
interfaces. `--json` and `--events-jsonl` are mutually exclusive, and event mode
does not apply to the read-only `update --check`. The closed
[schema](../carlina/core/installation-event-v1.schema.json) specifies the format.
This event mode currently requires an already stopped runtime: `--stop` and
`--systemd-unit` are refused before shutdown or ownership inspection. Those
options remain available in the ordinary maintainer interface without event mode.

Each event carries an operation ID, an increasing sequence number, the application
profile, the operation, the phase, the state, nullable byte counts, a stable error
category, whether the operation is retryable, and whether cooperative cancellation
is safe. It contains no arbitrary message, source URL, filesystem path or
exception text. Event-mode CLI error summaries go to stderr and do not include the
underlying exception text.

The default operation ID is a fresh UUID. A caller may supply a lowercase 32-hex
`--operation-id`. The journal and receipt record it alongside the existing,
independent transaction ID. Create a new lifecycle context for each attempt.
`--event-sequence-start N` continues after the last event number of a bootstrap.
`--event-terminal-file PATH` hands the terminal event to that caller instead of
stdout, while running events still stream normally. The destination must not
exist, and its parent directory must. The CLI writes it exclusively after action
cleanup, in the same closed event format and with private file permissions. A
failed handoff write can occur after activation, so receipts and journals
determine the installed state.

The installer adds no automatic retry or partial-download resume. A retry must
preserve the exact reviewed index bytes and requested version. `retryable` is not
permission to refresh a channel or select a different release. Download failures
include unavailable copies and signed size or hash mismatches, with atomic
cleanup of partial files. Signature and profile admission remain mandatory.

`--cancel-file PATH` requires event mode. The caller owns a request path that is
initially absent and requests cancellation by creating a regular file there. The
installer reads only its metadata and never creates or removes that control file.
Invalid control types and inspection failures abort at a safe boundary.
Cancellation is observed before target execution and between download chunks.
Current unpack, backup and target subprocesses finish their bounded step instead
of being killed. Once target code can access mutable data, a pending request no
longer interrupts initialisation, health checks, recovery or activation. The
operation can therefore finish successfully after a late cancellation request.
Force-killing the process is not part of this cancellation protocol, and existing
interrupted-operation journals still govern later stopped maintenance.

Rollback reads the prior generation and the paired recovery material from the
active receipt. It accepts cancellation before its first target-runtime probe.
From that probe onward, cancellation is unsafe, even when the probe does not
request data initialisation, so a late request lets the rollback or its recovery
finish. Paired full-root and database-only restores report the recovery phase. If
target verification fails, Carlina reactivates the pre-rollback data, or keeps an
unresolved rollback fence if reactivation fails. The event operation ID is
additional transaction and history metadata, not a replacement for the recovery
identity. An observer interrupt at the start of failure recovery is deferred until
the required data reactivation finishes. If that reactivation fails, the durable
rollback fence remains unresolved.

Terminal events follow transaction cleanup and lock release. Before target
execution, cancellation keeps the existing generation and retained recovery
material. Adoption does not restore untouched data merely because it created a
backup. An interrupted reconciliation preserves the previous transaction's journal
until recovery and archival finish. An unresolved recovery fence reports
`recovery_required`, disables retryability and must not be treated as a startable
previous generation. Ordinary observer errors derived from `Exception`, including
a broken event pipe, do not abort installation or recovery. Process interrupts
still follow the manager's cleanup and durable-recovery path. A caller that has
disconnected must inspect the durable state when it reconnects.

The generated shell and PowerShell entrypoints also provide an opt-in bootstrap
stream before Python is available. Shell uses `--events-jsonl`, and PowerShell
uses `-EventsJsonl`. Event mode requires `--skip-init --no-path-link` or
`-SkipInit -NoPathUpdate`, respectively. It performs managed adoption. The
separate interactive setup and user PATH integration remain with the caller.
Managed adoption keeps its compatibility, initialisation and recovery rules.

For example:

```bash
sh ./install-carlina-documents-linux-64.sh --events-jsonl --skip-init --no-path-link
```

```powershell
.\install-carlina-documents-win-64.ps1 -EventsJsonl -SkipInit -NoPathUpdate
```

The outer script generates one operation ID or accepts `--operation-id` or
`-OperationId`, then passes that ID and its last sequence to the installed
manager. It checks for handoff protocol version 1 before adoption. Older payloads
fail with `incompatible_release` instead of silently changing interfaces. The
manager holds its terminal event in the exclusive handoff file. The outer script
validates the identity, profile, operation, sequence and child exit status,
removes scratch, and then publishes that terminal event once. If the handoff is
absent, invalid or does not match the exit status, no terminal event is invented.
A disconnected caller must inspect durable state before another attempt.
Diagnostics use stderr.

Native `--cancel-file` or `-CancelFile` needs an existing, accessible parent
directory. The request file remains caller-owned, and Carlina never reads its
content. Event-mode outer downloads check for cancellation while waiting for
headers and between body reads. Shell stops and reaps its own curl child before
removing scratch or releasing the program lease. PowerShell uses cancellable HTTP
reads, and only its parent script writes the private output file, which is closed
before cleanup. Partial artifact byte counts are monotonic and stay below the
signed total. The final byte endpoint follows size and hash verification. Index
downloads have no invented total. Both paths restrict redirects to HTTPS.
PowerShell checks each resolved redirect before contacting it, with a
ten-redirect limit. Cancellation still waits for an active unpacking step and then
follows the manager's boundaries.

For `publisher-bootstrap-executable` payloads, event mode also reports scaffold
verification and expansion, each pinned dependency download and verification,
archive reconstruction and verification, and environment unpacking. Byte events
describe each download's start, partial bytes and verified completion. Totals
restart for each file instead of accumulating across dependencies. The outer
script remains the sole JSONL writer and the owner of the operation and sequence.
Publisher subprocesses use a closed, versioned record channel, with human and
native diagnostics on stderr. Missing, malformed or exit-mismatched records fail
before managed adoption.

Native publishers inspect the same caller-owned cancellation metadata while
waiting for headers and body reads and between safe substeps. Shell stops and
reaps the owned downloader. PowerShell closes the parent-owned output before
scratch cleanup and the publisher result. Their partial counts stay below the
signed total, and a complete endpoint requires exact size and hash verification.
The common v1 record grammar already represents these events. Older publisher
payloads can still report an unsafe download step, and the controller waits for
that step. Native unpacking finishes its current step. There is no
partial-download resume. Shell event-mode outer downloads use a 30-second
connection timeout and a 900-second request limit. PowerShell enforces a
900-second total limit across headers, redirects and body. A timeout is a
retryable download failure, distinct from caller cancellation. Publisher event
downloads keep their pinned size and hash inputs, eight-redirect limit and
900-second total deadline. Ordinary publisher paths keep their existing interfaces
and timeouts. The Windows publisher rejects credentialed redirects before
contacting them.

Linux progress-mode publisher downloads require util-linux `prlimit`. Their single
downloader child applies an exact per-file `size + 1` bound, ignores `SIGXFSZ` so
that a capped write fails without a signal core dump, and disables core dumps.
These settings stay in that child. The limits of the parent and the unpacker are
unchanged. Missing `prlimit` fails preflight before any download. An unresolved
download child preserves its scratch and suppresses the publisher result. Neither
subprocess success nor a byte counter completes the public operation: success
requires managed adoption, and terminal outcomes follow outer cleanup. On POSIX,
if the private publisher sequence cursor is lost, the terminal event is suppressed
so that the parent cannot repeat an older sequence. Inspect durable state after
that transport failure.

The installed-Python applying-update event path consumes the same publisher
records. Its owner thread emits running events under the existing operation ID and
sequence. Publisher results do not become separate public terminals. Unknown,
missing or exit-mismatched records fail before target-runtime initialisation.
Valid dependency-download and verification failures keep their specific error
codes, but an existing recovery fence takes precedence.

The controller samples cancellation requests during transfers and mirrors them to
an exclusive, empty control file in private program-volume scratch. The publisher
observes that file at its next safe boundary. The caller's request file remains
unchanged. Observer interrupts and control errors are deferred until the child's
output has been drained and the process reaped. The manager keeps its program and
data leases through that wait. It then removes scratch and quarantines an
incomplete candidate before emitting a terminal event. A failure before
initialisation retains the backup and the prior active generation without
restoring untouched data.

In this opt-in publisher path, the 3,600-second unpack budget requests a
cooperative stop. The current transfer or native unpacking step finishes before
ownership is released, so this is not a hard process-kill deadline. Ordinary
subprocess behaviour without the lifecycle, and artifact calls that do not involve
a publisher, keep their existing contracts. Qualification of native platforms and
installed artifacts remains a separate release gate. POSIX permission-bit checks
are separate from Windows ACL acceptance: Windows `chmod` controls the read-only
attribute ([Python documentation](https://docs.python.org/3.14/library/os.html#os.chmod)).

The generated index hash and exact version stay fixed across attempts, even when
the contents behind the URL change. Use a fresh operation ID for each attempt.
Native entrypoints do not resume partial downloads. A rerun can reconcile a
matching ready generation, and unfinished unpacking stays preserved for explicit
recovery. First-run setup and graphical desktop consumers remain later release
work. A successful adoption does not claim that a fresh data root has completed
onboarding.

Native entrypoints acquire `state/managed-install.lock` before creating scratch or
generation paths. POSIX entrypoints require `flock`, and PowerShell holds an
exclusive `FileStream`. Both exclude the manager's `FileLock` on the tested local
Linux filesystem, including portable PowerShell. Acceptance of native Windows
sharing and ACLs, and of the target filesystem, remains a separate gate. POSIX
cleanup closes its descriptor without explicitly unlocking it, which preserves
exclusion while an inheriting child still runs
([flock semantics](https://man7.org/linux/man-pages/man2/flock.2.html)).

An exclusively created `.carlina-bootstrap-claim` records a random nonce and a
digest of the profile, version, channel, exact index bytes and signed artifact
contract. After unpacking and the profile checks, bundled Python reads the normal
root configuration and exclusively binds the canonical program and data roots in
`.carlina-bootstrap-binding.json`. This readiness step does not create the data
root. The identity argument uses validated base64 so that PowerShell Legacy native
argument handling cannot remove its JSON quotes. Missing protocol support or
inconsistent metadata stops the process before adoption. The outer wrapper
releases its program lease only after readiness, and then the manager reacquires
its normal program and data leases. Marked adoption validates the artifact bytes,
claim, binding and exact index hash before recovery or archival. Every manager
recovery entrypoint also checks native journal ownership under the leases,
including interruption before the ordinary installation metadata was written. A
different data root cannot become the destination of an old paired restore.

Cleanup may remove a failed pre-adoption tree only while holding its program lease
and matching its own exact claim. Once adoption starts, it retains the generation,
because a child can commit before its exit is observed. If the outcome of a native
child is unobserved or the claim has changed, scratch and the generation are
preserved and a terminal claim is suppressed. The manager never automatically
quarantines a native-marked generation. Its journal and receipt preserve
`bootstrap_claim_id` and the original `bootstrap_index_sha256`. Later
authenticated update indexes may reuse the same artifact without rewriting that
original binding. Explicit retry checks those fields against the existing
immutable evidence. Unmarked legacy generations keep their existing manager
interface. Claim enforcement requires the protocol-aware entrypoints and manager.
It does not retrofit ownership checks into an older installed manager. Ready
reruns additionally require `BOOTSTRAP_RESUME_PROTOCOL_VERSION = 1`. A payload
without that capability stops before the manager handoff. Acceptance on native
Windows and of mixed-version installed artifacts remains a separate gate.

### Native ready-generation reruns

Rerunning the same carrier first acquires the program lease and reads the exact
claim frame. A regular, bounded binding must already exist. A missing binding,
foreign identity, link or incomplete generation is refused without unpacking or
deleting it. The carrier verifies fresh copies of its frozen index and artifact
and checks the profile. It then validates the existing binding against the
normally resolved roots, without creating or replacing a binding. The signed
inputs, the generation and the original claim remain fixed, and each event attempt
has a new operation ID.

The carrier passes `--resume-bootstrap-claim` with its nonce. The manager chooses
the original transaction only after taking both program and data leases and
validating the ready generation. A matching adoption journal uses the explicit
retained retry checks below. A proven commit returns the original result without
target execution. A recognised stopped attempt creates a linked transaction
through the existing recovery. The native wrapper does not read a journal outside
those leases to guess a retry ID.

Without a journal, ordinary adoption requires a forward release, and neither the
pointer nor a receipt may indicate candidate activation. A different prior journal
may be archived only if it records a completed transaction bound to the same roots
and profile, has a matching committed receipt and current pointer, and has no
pending recovery fences. That branch also requires a forward candidate with no
receipt of its own. A candidate that already has a receipt needs the matching
original adoption transaction or the separately validated managed-update reuse
path. Automatic fresh adoption does not overwrite its evidence. Unrelated failed
or in-flight journals, missing ownership and ambiguous commit state remain
untouched. Explicit `--retry-transaction` and `--resume-bootstrap-claim` cannot be
combined, and both require an exact version and index hash.

Ready reruns never unpack into the retained generation and never delete it on
failure. Their new download scratch is cleaned after the child is observed to
complete. Scratch from earlier attempts remains retained, because the current
claim does not bind each old scratch path to a reaped child. The rerun does not
enumerate or remove unattributed directories. Unproven preparing generations
remain retained, and neither PID guesses nor an available parent lock authorise
removal. The [custody](#native-preparation-custody) and
[failed-preparation retry](#failed-preparation-retry-before-managed-adoption)
contracts define the cases that have sufficient completion evidence.

### Native preparation custody

Standard runtime bundles and Documents publisher payloads have an opt-in
preparation custodian. Use `--supervised-preparation` on Linux or
`-SupervisedPreparation` on Windows, together with event mode and its existing
skip-init and no-path-update options. Publisher admission stays limited to
Documents. Ordinary carrier interfaces keep their existing behaviour.

Linux requires cgroup v2, systemd tooling version 254 or later and an accessible
systemd user manager. Supervised publisher progress also requires a `tail`
implementation that supports `--pid` and `--sleep-interval`. Windows requires
64-bit Windows PowerShell Desktop 5.1 and Windows 10 or later, and Windows 11
remains the release target. Supervised execution is refused when a capability is
missing. This source interface is not clean-machine installer qualification.

The separate custodian retains a copy of the same open maintenance lease while the
controller keeps its original. There is no unlock and reacquire handoff before
writer admission. On Linux, the custodian stays outside the unpacker's user scope
and waits for its cgroup to become empty after the direct unpacker exits. The
scope's entry process closes its lease descriptor, so detached descendants cannot
create a false completion by dropping that descriptor. On Windows, the custodian
explicitly inherits the duplicated lease handle and owns an unnamed job. Native
process creation assigns that job before execution, so descendants cannot break
away. The custodian waits for both the direct process handle and zero active job
members. Closing the custodian's last job handle terminates the job's members but
never publishes a completion record. The job contains only native preparation.

Publisher stdout goes to an owned regular-file spool. A separate observer feeds
the existing private-record parser and the carrier's single public event stream,
but it cannot certify custody or completion. Native stdout no longer depends on
the pipe of the controller or observer. After the direct process and all
descendants finish, the custodian validates the complete ASCII LF/CRLF protocol,
the bounded records and the agreement between the result and the native exit
before publishing completion. Missing, malformed or mismatched results produce no
reusable successful completion, even when the native exit code is zero.
Cancellation remains cooperative through the exact caller-owned control path, and
no hard termination is added. Windows uses its fixed Desktop PowerShell executable
to run the verified publisher script.

Native probes confirmed that Windows can remove a job's name after its last handle
closes while a member still writes. Microsoft's
[object lifetime contract](https://learn.microsoft.com/en-us/windows-hardware/drivers/kernel/life-cycle-of-an-object)
distinguishes handles, references and names. A continuously held job handle keeps
membership queryable, and creation must assign the child before execution through
the
[job-list process attribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).
The
[atomic creation example](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812)
explains why creating a suspended process and then assigning it leaves a window in
which an interruption can leave the child unassigned. Neither a failed name lookup
nor a termination request is a completion barrier.

Windows scratch uses a protected inheritable DACL for the current user, SYSTEM and
administrators before the custodian's context and source are written. Windows
compiles the trusted helper using `csc.exe` from its Framework runtime directory
and a private UTF-8 response file. Inputs, assembly and diagnostics stay in the
owned preparation scratch, and the compiler is waited for before those bytes are
loaded. The custodian loads the same embedded assembly bytes without compiling
again. This supports the tested Unicode compiler temp without changing process
temp settings or compiler security settings. The custodian then gives the unpacker
its owned extraction `TEMP`/`TMP`. Complete native program-path acceptance remains
unqualified and must be resolved before claiming that clean-machine baseline.

Linux uses a
[user scope](https://www.freedesktop.org/software/systemd/man/latest/systemd.scope.html)
and the kernel's
[cgroup population state](https://docs.kernel.org/admin-guide/cgroup-v2.html). The
probe tracked a detached descendant after both its direct parent and the
controller had exited, including after the inherited lock descriptors closed.
Trusted unpackers remain trusted code, and these mechanisms establish lifecycle
custody.

After observed writer completion and an exact claim recheck, the custodian
publishes `.carlina-bootstrap-preparation` atomically without replacing an
existing path. Its five LF-terminated ASCII lines are
`carlina.bootstrap-preparation/1`, the original lowercase claim nonce, the
authenticated identity digest, the canonical standard Base64 of the UTF-8 program
root, and the unsigned decimal native exit code. The frame is bounded to 16,384
bytes, the root bytes to 8,192 and the exit code to uint32. Parsing rejects CRLF,
noncanonical encodings and extra data. A zero code records completed native
extraction. It does not assert profile, environment or data readiness.

Only the explicit new mode accepts a retained successful preparation in place of
an existing ready binding. It still verifies its frozen index and artifact, checks
the runtime profile and binds the normally resolved roots before the manager
handoff. The runtime must expose preparation protocol 1. A write-once
`.carlina-bootstrap-preparation-binding.json` intent precedes the ready binding,
and a retry may recreate a missing ready binding only for that intent's exact
roots. Adding a preparation record to an old ready generation cannot reclassify
it. The carrier forwards the original claim to the existing managed resume path,
with one outer public event owner. Initialisation, migrations, activation and
rollback keep their current journals and leases.

A controller can exit while the custodian completes extraction and publishes its
record. An observed custodian exit is accepted only with a matching record and
native and host exit status. Lost custody, missing or malformed evidence, old
preparing claims and unattributed scratch remain preserved. An enclosing launcher
job can terminate the Windows custodian along with the controller. That counts as
custodian loss, with no completion guarantee. The graphical launcher's process and
job topology still needs its own acceptance. Removal is never authorised by PID or
age guesses, a free lease or a missing named object.

### Failed preparation retry before managed adoption

An explicit supervised rerun can recover a matching nonzero completion in an
unadopted root. It first verifies fresh copies of its frozen index and artifact.
Under the retained program lease, it validates the claim, identity, root and
completion again, and then retires the whole failed generation to
`state/preparation-retired/<version>.<claim>`. The original claim, the completion
and all failed bytes remain there. It creates a fresh claim at the normal version
path and runs native preparation again. This is a new extraction, not a
ready-generation resume. Ordinary installer calls keep their existing interface.

The native path refuses any current or previous pointer, any candidate ready
binding or binding intent, any generation receipt (including malformed or link
entries), and any state entry other than `managed-install.lock` and the private
retirement directory. Recognised or unknown manager state keeps this native path
closed. In that case, use managed recovery or inspect the retained evidence. This
interface adds no automatic retirement of native-marked trees in managed roots.
Successful completion records continue through normal profile and root validation
and adoption. A missing, malformed or foreign completion never authorises
retirement. Old scratch remains preserved because the completion frame does not
bind it.

Retirement uses a same-filesystem rename without replacement or a copy fallback.
Linux requires GNU `mv` with `--no-copy`, `--no-clobber` and
`--no-target-directory`, and Windows uses `Directory.Move`. See the
[GNU move contract](https://www.gnu.org/software/coreutils/manual/html_node/mv-invocation.html)
and the
[.NET directory move contract](https://learn.microsoft.com/en-us/dotnet/api/system.io.directory.move).
The private retirement directory must have the expected ownership and permissions.
Existing destinations and unsafe links remain untouched. Cancellation is safe
before the rename. A request made during the rename takes effect afterwards,
before unpacking. If an interruption happens after retirement and before the new
claim, the retired generation stays intact and the next attempt can create its
fresh generation. This operation does no initialisation, migration, activation,
rollback or deletion of retired material.

### Retained adoption retry

The manager's `install` command accepts `--expected-index-sha256` to require the
exact release-index bytes after signature and profile admission. Explicit retry
also requires `--retry-transaction` and `--version`. Resolve these values from the
original reviewed inputs and the adoption journal, and use a trusted manager that
implements this interface after stopping the managed runtime:

```bash
CARLINA_HOME="$DATA_ROOT" carlina install \
  --program-root "$PROGRAM_ROOT" \
  --manifest "$FROZEN_INDEX" --version "$VERSION" \
  --expected-index-sha256 "$INDEX_SHA256" \
  --artifact "$ARTIFACT" --adopt-version-dir "$VERSION_DIR" \
  --retry-transaction "$TRANSACTION_ID" --events-jsonl
```

Signature, profile, raw index hash, and artifact hash and size checks still apply.
Under the program and data maintenance leases, retry validates the original
transaction, canonical roots, version, signer and release contract before recovery
or journal archival. New adoption journals and receipts record their program and
data roots. Retry reads bounded regular metadata files and rejects duplicate keys
and linked generation paths. Older unbound records require manual inspection,
while ordinary adoption and update interfaces remain available.

A current pointer and a matching committed receipt permit readback of the original
transaction without target initialisation, backup, restore or launcher rewriting.
The attempt uses a fresh event operation ID. A recognised stopped attempt instead
uses the existing recovery, archives the original journal and adopts the retained
runtime under a new transaction linked to the prior one. The original backup and
recovery evidence remain retained, and the retry does not download or unpack
again.

In-flight target-probe and migration records, unknown activation outcomes,
conflicting ownership and unrelated recovery fences remain blocked. The native
scripts do not use these controls yet. A directory containing Python is not enough
evidence for an automatic bootstrap retry.

For updates and adoption, the current-pointer switch is the activation commit. An
exception after a proven commit still propagates, and the committed data and the
matching active receipt stay intact. Unreadable or conflicting commit evidence
retains a recovery fence instead of authorising a restore beneath an active
target.

### Apply and recover

Read-only preflight:

```bash
carlina update --check
```

Legacy indexes default to full recovery. Review a proportional choice before
both preflight and apply, for example:

```bash
carlina update --check \
  --recovery-coverage database_only \
  --recovery-reason "Reviewed update intends to mutate SQLite only"
carlina update --stop --systemd-unit carlina.service \
  --recovery-coverage database_only \
  --recovery-reason "Reviewed update intends to mutate SQLite only"
```

`none` uses the same explicit audit fields and leaves database, filesystem and
external effects uncovered. An unattended narrower selection additionally requires
`--recovery-unattended --recovery-policy-id POLICY`. The actor defaults to the
local user or can be set with `--recovery-actor`.

Before target execution under `none`, Carlina durably arms a recovery-required
record, because it cannot prove that the prior runtime will still match changed
data. If the target does not commit, including after a hard process death,
Carlina refuses a managed restart or another unacknowledged update. After
reviewing the exact record, deploy a compatible target with:

```bash
carlina update ... \
  --acknowledge-recovery-required TRANSACTION_ID
```

The fence resolves only after that target activates successfully. Restoring and
reactivating a database checkpoint uses a durable switch witness. Process death
between the SQLite, WAL and SHM renames is recovered before any later update can
archive the failed journal. Rollback adds a transaction above the physical data
switch. Until the prior program pointer commits, a hard death after either
full-root or SQLite-only restoration reactivates the data that belongs to the
still-current program. A hard death during target execution, including adoption or
a first installation with no prior program pointer, is likewise fenced. The next
stopped maintenance command restores the selected full or checkpoint material
before archiving that journal. Managed start performs no recovery itself and
refuses every non-terminal switch. Stopped maintenance performs the
reconciliation.

Current apply path:

```bash
carlina update --stop --systemd-unit carlina.service
carlina doctor
systemctl --user start carlina.service
```

For a managed Linux user service, `--systemd-unit` is valid only with `--stop`.
Carlina verifies exact PID and cgroup ownership, stops through user systemd,
requires `MainPID=0` and a stable stopped state, and rejects runtime
reacquisition. It never invokes system-level systemd. `update --check --stop` is
invalid because preflight must remain read-only.

The ordinary updater for opaque or mutating releases:

1. acquires the managed-install lock and data-root lease;
2. downloads and verifies the exact signed artifact;
3. creates and verifies the selected recovery material when a database exists:
   complete recovery automatically, or an explicit database checkpoint or none;
4. unpacks or reuses an authenticated immutable program version;
5. runs target identity, migrations, built-in reconciliation, and Doctor and
   readiness checks;
6. restores protected data automatically if target execution fails;
7. writes the receipt and launcher; and
8. atomically changes `current` only after success.

Database-only and none selections already classify before payload estimation. The
implemented v2 schema can additionally authenticate impact. On Linux, a trusted v2
`none` release takes a separate path. First, bwrap passes its explicit namespace
and mount capability self-test. Candidate dependency tracing then runs in a
preliminary sandbox with an empty root and no network. The target generation runs
its fixed probe with only the exact resolved loader libraries and writable tmpfs
scratch. The host validates and records the closed attestation, and the updater
writes the receipt and changes the pointer. This path does not size or create a
backup or checkpoint, invoke target Pixi or bootstrap, run migrations or
reconciliation, call Doctor or readiness, or expose the live data and
program-state roots. A failure before pointer activation leaves the old release
restartable.

Windows and unsupported Linux hosts reject the same v2 `none` contract at
preflight. They never fall back to a disposable environment variable or the
ordinary live target probe. Current published indexes remain v1, so the public and
private channels do not select this path until explicit dual publication is
implemented.

## Add-on update

Local ZIP and signed-catalog installs share one coordinator. They validate and
compile without importing the code, review permissions and dependencies, stage
assets, publish an immutable `<addon-id>/<version>` generation, run declared
lifecycle work, and select the generation atomically. Failure restores the
previous selected generation and activation state. A change to code or UI alone
does not create a whole-application backup. Owned data, Connections, secrets,
preferences and bindings live outside the immutable generation.

Purge deletes owned state and therefore defaults to full recovery. Program-tree
packages remain on the managed-core path until the accepted ownership-transfer
transaction is implemented and committed for that package.

## Backup formats

```bash
carlina backup --json
```

Version 1 is a complete anchor: a consistent SQLite snapshot plus selected owned
files, schema heads, versions, hashes, sizes and key identifiers. Version 2 is a
direct one-level overlay over a verified neighbouring v1 base. It binds that base,
carries the complete logical file table and the changes and tombstones, and stores
only changed members. Overlay chains are forbidden, and a restore needs both
files.

Version 2 saves writes and retained bytes, not traversal cost. A referenced base
must not be removed.

## Restore and rollback

Plan a restore:

```bash
carlina restore /absolute/path/to/carlina-backup-ID.zip
```

Restore requires the printed confirmation token and a stopped service. Carlina
extracts into a staging root, rejects unsafe or link members and anything over
budget, verifies hashes and database compatibility, preserves machine-local
configuration and key bindings, creates a safety root, and switches atomically.
Backups omit raw recovery keys, so the matching master or encrypted recovery
material is still required.

Program-tree add-on versions can require a paired data restore even when the core
database schema is compatible. Target initialisation refuses an implicit built-in
add-on downgrade. Use the paired archive when the release requires a restore or
when the older runtime cannot reconcile the recorded add-on versions:

```bash
carlina rollback --stop --systemd-unit carlina.service --restore-data
```

Never manually edit `current` or a receipt to force a program-only rollback across
data effects.

## Interrupted operations and retention

Inspect before acting:

```bash
carlina status --json
carlina doctor
carlina update --check
```

`doctor --deep` hashes the latest recovery archive and, for an overlay, its
complete base. Run it when auditing recovery material, before a restore, or on a
periodic continuity schedule, not after every source or add-on deployment.

Retain failed release directories, staging, receipts, journals and recovery
material until the supported path, or a source-level diagnosis, proves what is
safe. Retention is based on reference reachability across receipts, rollback
windows, restores, migration and private-overlay journals, pins and overlay
closure, not on filename age.

No reference-aware prune command exists yet. Delete only an exact archive, after
manually proving it unreachable and verifying the retained recovery set. Backups
on the same disk support rollback but not disk loss. Keep a periodic verified
complete anchor and matching encrypted key recovery in a separate protected
location, and test restore against a disposable root.

`carlina storage report` includes the managed program recovery directory as
`managed_recovery`, so receipt-bound database checkpoints are visible even though
they live outside the data root.
