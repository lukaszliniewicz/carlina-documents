<script lang="ts">
  import { onDestroy, untrack } from "svelte";
  import type {
    FrontendAdministrationMutationResponse,
    FrontendApiClient,
    FrontendContinuitySettings,
  } from "@carlina/sdk";
  import { Badge, Button, Dialog, Icon } from "@carlina/ui";
  import {
    readRestartStatus,
    startRestartReconnect,
    type RestartStatus,
  } from "../../../lib/restart-reconnect.ts";

  interface Props {
    api: FrontendApiClient;
    value: FrontendContinuitySettings;
    onrefresh: () => Promise<void>;
  }

  let { api, value, onrefresh }: Props = $props();

  let current = $state(untrack(() => value));
  let selectedFilename = $state(
    untrack(() => value.backups[0]?.filename ?? ""),
  );
  let createReviewOpen = $state(false);
  let restoreReviewOpen = $state(false);
  let restoreConfirmation = $state("");
  let working = $state(false);
  let restartRequired = $state(false);
  let restarting = $state(false);
  let restartPending = $state(false);
  let restartStatusMessage = $state("");
  let stopRestartReconnect: (() => void) | undefined;
  let error = $state("");
  let notice = $state("");

  onDestroy(() => {
    stopRestartReconnect?.();
  });

  function formatBytes(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(
      Math.floor(Math.log(bytes) / Math.log(1024)),
      units.length - 1,
    );
    const value = bytes / 1024 ** index;
    return `${new Intl.NumberFormat(undefined, {
      maximumFractionDigits: value >= 10 ? 0 : 1,
    }).format(value)} ${units[index]}`;
  }

  function formatDate(value: string | null): string {
    if (!value) return "No recovery point";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "Date unavailable";
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  }

  async function createBackup(): Promise<void> {
    working = true;
    error = "";
    notice = "";
    try {
      const response =
        await api.request<FrontendAdministrationMutationResponse>(
          current.actions.create_backup_url,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ acknowledge: true }),
          },
        );
      if (response.continuity) {
        current = response.continuity;
        selectedFilename =
          response.continuity.backups[0]?.filename ?? "";
      }
      notice = response.message || "Complete backup created.";
      createReviewOpen = false;
      await onrefresh();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The complete backup was not created.";
    } finally {
      working = false;
    }
  }

  function openRestore(): void {
    if (!selectedFilename) return;
    restoreConfirmation = "";
    restoreReviewOpen = true;
  }

  async function restoreBackup(): Promise<void> {
    if (restoreConfirmation.trim() !== selectedFilename) return;
    working = true;
    error = "";
    notice = "";
    try {
      const response =
        await api.request<FrontendAdministrationMutationResponse>(
          current.actions.restore_backup_url,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              filename: selectedFilename,
              confirmation: restoreConfirmation,
            }),
          },
        );
      notice = response.message || "Restore verified and scheduled.";
      restartRequired = Boolean(response.requires_restart ?? true);
      restoreReviewOpen = false;
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The restore was not scheduled.";
    } finally {
      working = false;
    }
  }

  async function restartServer(): Promise<void> {
    restarting = true;
    restartPending = true;
    restartStatusMessage = "Requesting a managed restart…";
    error = "";
    notice = "";
    try {
      const response = await api.request<{
        success?: boolean;
        message?: string;
        handoff_id?: string;
      }>(
        "/api/system/restart",
        { method: "POST" },
      );
      if (typeof response.handoff_id !== "string" || !response.handoff_id) {
        throw new Error(
          "The server did not confirm a durable restart handoff.",
        );
      }
      notice =
        response.message ||
        "Server restart requested. Waiting for the managed successor.";
      stopRestartReconnect?.();
      stopRestartReconnect = startRestartReconnect({
        handoffId: response.handoff_id,
        readStatus: () =>
          readRestartStatus((path, init) =>
            api.request<RestartStatus>(path, init),
          ),
        onStatus: (state) => {
          restartPending = true;
          if (state.status === "unknown") {
            const reason = state.reasons?.[0];
            restartStatusMessage = reason
              ? `The restart status could not be verified (${reason}).`
              : "The restart status could not be verified yet.";
            error = restartStatusMessage;
            return;
          }
          error = "";
          if (
            state.completed_handoff_id &&
            state.completed_handoff_id !== response.handoff_id
          ) {
            restartStatusMessage =
              "Another restart completed; still waiting for this request.";
            return;
          }
          restartStatusMessage = state.pending
            ? "Waiting for the managed successor to become ready…"
            : "Waiting for confirmation from the managed successor…";
        },
        onSuccessor: () => window.location.reload(),
        onTimeout: () => {
          restarting = false;
          restartPending = false;
          restartStatusMessage = "";
          error =
            "The restart is taking longer than expected and has not been confirmed. Keep this page open and try again when the server is reachable.";
        },
        schedule: (callback, delayMs) => window.setTimeout(callback, delayMs),
        cancel: (timer) => window.clearTimeout(timer),
      });
    } catch (reason) {
      stopRestartReconnect?.();
      stopRestartReconnect = undefined;
      restartPending = false;
      restartStatusMessage = "";
      error =
        reason instanceof Error
          ? reason.message
          : "The server restart could not be requested.";
      restarting = false;
    }
  }
</script>

<section class="continuity-panel" aria-labelledby="continuity-heading">
  <header class="section-intro">
    <div>
      <p>Continuity ledger</p>
      <h2 id="continuity-heading">Backup and recovery</h2>
    </div>
    <span>
      Create integrity-checked archives of the database and owned files. Restore
      is verified first, then activated atomically during a managed restart.
    </span>
  </header>

  {#if error}
    <p class="panel-message panel-message--error" role="alert">{error}</p>
  {:else if notice}
    <p class="panel-message" role="status">
      <Icon name="check" size={14} />
      {notice}
    </p>
  {/if}

  {#if restartRequired}
    <section class="restart-banner" role="status">
      <Icon name="refresh" size={19} />
      <div>
        <strong>A restart is required before anyone continues.</strong>
        <p>
          The selected archive is verified but the live state is unchanged. The
          restart creates a safety point and performs the atomic switch.
        </p>
        {#if restartPending || restartStatusMessage}
          <p class="restart-status" aria-live="polite">
            {restartStatusMessage}
          </p>
        {/if}
      </div>
      <Button
        disabled={restarting || restartPending}
        onclick={() => void restartServer()}
      >
        {restarting ? "Restarting…" : "Restart server"}
      </Button>
    </section>
  {/if}

  <section class="continuity-reading" aria-label="Continuity summary">
    <article>
      <p>Live database</p>
      <strong>{formatBytes(current.database.size_bytes)}</strong>
      <Badge tone={current.database.available ? "success" : "danger"}>
        {current.database.available ? "Available" : "Missing"}
      </Badge>
    </article>
    <article>
      <p>Recovery points</p>
      <strong>{current.summary.backup_count}</strong>
      <span>verified complete archives</span>
    </article>
    <article>
      <p>Latest backup</p>
      <strong>{formatDate(current.summary.latest_backup_at)}</strong>
      <span>stored in the private Carlina data root</span>
    </article>
  </section>

  <div class="continuity-workspace">
    <section class="backup-ledger">
      <header>
        <div>
          <p>Recovery catalogue</p>
          <h3>Available backups</h3>
        </div>
        <Button
          disabled={working || !current.database.available}
          onclick={() => (createReviewOpen = true)}
        >
          <Icon name="plus" size={14} />
          New backup
        </Button>
      </header>

      {#if current.backups.length}
        <div class="backup-list">
          {#each current.backups as backup (backup.filename)}
            <label class:active={selectedFilename === backup.filename}>
              <input
                type="radio"
                name="recovery-point"
                value={backup.filename}
                checked={selectedFilename === backup.filename}
                onchange={() => (selectedFilename = backup.filename)}
              />
              <span class="backup-mark" aria-hidden="true"></span>
              <span>
                <strong>{formatDate(backup.created_at)}</strong>
                <small>{backup.filename}</small>
              </span>
              <em>{formatBytes(backup.size_bytes)}</em>
            </label>
          {/each}
        </div>
        <footer>
          <p>
            Activation replaces owned state only after verification and restart.
          </p>
          <Button
            variant="danger"
            disabled={!selectedFilename || working || restartRequired}
            onclick={openRestore}
          >
            <Icon name="refresh" size={14} />
            Restore selected
          </Button>
        </footer>
      {:else}
        <div class="empty-ledger">
          <Icon name="file" size={24} />
          <h3>No recovery points yet</h3>
          <p>Create a verified snapshot before structural maintenance.</p>
        </div>
      {/if}
    </section>

    <aside class="migration-reading">
      <div class="migration-mark">
        <Icon name="shield" size={23} />
      </div>
      <p>Migration boundary</p>
      <h3>Plaintext export retired</h3>
      <p>{current.migration_policy.note}</p>

      <dl>
        <div>
          <dt>Plaintext browser export</dt>
          <dd>Unavailable</dd>
        </div>
        <div>
          <dt>User-deleting migration</dt>
          <dd>Retired</dd>
        </div>
        <div>
          <dt>New encrypted writes</dt>
          <dd>Installation-key protected</dd>
        </div>
      </dl>

      <div class="migration-note">
        <strong>Why this changed</strong>
        <p>
          The classic action deleted every account except the operator before
          writing plaintext. Phase 7 deliberately does not reproduce it.
        </p>
      </div>
    </aside>
  </div>
</section>

<Dialog
  open={createReviewOpen}
  title="Create a complete recovery point?"
  description="SQLite is snapshotted through its online backup API, owned files are hashed, and the archive is verified before publication."
  onclose={() => {
    if (!working) createReviewOpen = false;
  }}
>
  <div class="dialog-copy">
    <Icon name="file" size={22} />
    <p>
      This is non-destructive. The archive remains inside the private data root,
      contains application data, and deliberately excludes recovery keys.
    </p>
  </div>
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={working}
      onclick={() => (createReviewOpen = false)}
    >
      Cancel
    </Button>
    <Button
      disabled={working}
      onclick={() => void createBackup()}
    >
      {working ? "Creating…" : "Create backup"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={restoreReviewOpen}
  title="Schedule this complete restore?"
  description="The archive is verified now. A managed restart later switches owned state atomically while retaining the previous root as safety state."
  onclose={() => {
    if (!working) restoreReviewOpen = false;
  }}
  size="lg"
>
  <div class="restore-reading">
    <p>Selected recovery point</p>
    <strong>{selectedFilename}</strong>
  </div>
  <label class="confirmation-field" for="restore-confirmation">
    Type the exact filename to continue
    <input
      id="restore-confirmation"
      bind:value={restoreConfirmation}
      autocomplete="off"
      spellcheck="false"
    />
  </label>
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={working}
      onclick={() => (restoreReviewOpen = false)}
    >
      Keep current database
    </Button>
    <Button
      variant="danger"
      disabled={
        working ||
        restoreConfirmation.trim() !== selectedFilename
      }
      onclick={() => void restoreBackup()}
    >
      {working ? "Verifying…" : "Verify and schedule restore"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .continuity-panel {
    border-inline: 1px solid var(--color-rule-strong);
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .section-intro {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.6fr);
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.4rem;
  }

  .section-intro p,
  .section-intro h2,
  .section-intro > span {
    margin: 0;
  }

  .section-intro p,
  .backup-ledger header p,
  .migration-reading > p:first-of-type {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .section-intro h2 {
    margin-top: 0.25rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.4rem, 2.7vw, 2rem);
    font-weight: 520;
  }

  .section-intro > span {
    align-self: end;
    color: var(--color-ink-muted);
    font-size: 0.73rem;
    line-height: 1.6;
  }

  .panel-message {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1.4rem;
    color: var(--color-lichen);
    font-size: 0.7rem;
  }

  .panel-message--error {
    color: var(--color-danger);
  }

  .restart-banner {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.8rem;
    border-bottom: 1px solid var(--color-clay);
    padding: 0.9rem 1.2rem;
    background: var(--color-clay-muted);
  }

  .restart-banner strong,
  .restart-banner p {
    margin: 0;
    font-size: 0.69rem;
  }

  .restart-banner p {
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
  }

  .restart-banner .restart-status {
    color: var(--color-ink);
    font-weight: 650;
  }

  .continuity-reading {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .continuity-reading article {
    min-width: 0;
    display: grid;
    gap: 0.25rem;
    border-right: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
  }

  .continuity-reading article:last-child {
    border-right: 0;
  }

  .continuity-reading p,
  .continuity-reading span {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.63rem;
  }

  .continuity-reading strong {
    overflow-wrap: anywhere;
    font-family: var(--font-editorial);
    font-size: 1.15rem;
    font-weight: 540;
  }

  .continuity-workspace {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(17rem, 0.32fr);
  }

  .backup-ledger {
    min-width: 0;
    border-right: 1px solid var(--color-rule-strong);
  }

  .backup-ledger > header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem 1.15rem;
  }

  .backup-ledger header p,
  .backup-ledger header h3 {
    margin: 0;
  }

  .backup-ledger header h3 {
    margin-top: 0.2rem;
    font-family: var(--font-editorial);
    font-size: 1.3rem;
    font-weight: 520;
  }

  .backup-list label {
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem 1.1rem;
    cursor: pointer;
  }

  .backup-list label:hover,
  .backup-list label.active {
    background: var(--color-clay-muted);
  }

  .backup-list input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
  }

  .backup-mark {
    width: 0.9rem;
    height: 0.9rem;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .backup-list label.active .backup-mark {
    border: 3px solid var(--color-paper);
    outline: 1px solid var(--color-clay);
    background: var(--color-clay);
  }

  .backup-list label > span:nth-of-type(2) {
    min-width: 0;
    display: grid;
    gap: 0.22rem;
  }

  .backup-list strong,
  .backup-list small {
    overflow-wrap: anywhere;
  }

  .backup-list strong {
    font-size: 0.72rem;
  }

  .backup-list small,
  .backup-list em {
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
    font-style: normal;
  }

  .backup-ledger footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.9rem 1.1rem;
    background: var(--color-surface);
  }

  .backup-ledger footer p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
  }

  .empty-ledger {
    display: grid;
    justify-items: start;
    gap: 0.45rem;
    min-height: 16rem;
    align-content: center;
    padding: 2rem;
  }

  .empty-ledger h3,
  .empty-ledger p {
    margin: 0;
  }

  .empty-ledger h3 {
    font-family: var(--font-editorial);
    font-size: 1.25rem;
    font-weight: 520;
  }

  .empty-ledger p {
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  .migration-reading {
    min-width: 0;
    padding: clamp(1.2rem, 3vw, 1.8rem);
    background:
      linear-gradient(145deg, var(--color-lichen-muted), transparent 58%),
      var(--color-surface);
  }

  .migration-mark {
    width: 3.2rem;
    height: 3.2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-lichen);
    color: var(--color-lichen);
  }

  .migration-reading > p:first-of-type {
    margin: 1.2rem 0 0.25rem;
  }

  .migration-reading h3 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.4rem;
    font-weight: 520;
  }

  .migration-reading > p:nth-of-type(2) {
    margin: 0.65rem 0 1.1rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.55;
  }

  dl {
    margin: 0;
    border-block: 1px solid var(--color-rule);
  }

  dl div {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0;
    font-size: 0.63rem;
  }

  dl div:last-child {
    border-bottom: 0;
  }

  dt {
    color: var(--color-ink-muted);
  }

  dd {
    margin: 0;
    text-align: right;
  }

  .migration-note {
    border-left: 3px solid var(--color-clay);
    margin-top: 1.2rem;
    padding-left: 0.8rem;
  }

  .migration-note strong,
  .migration-note p {
    margin: 0;
    font-size: 0.66rem;
  }

  .migration-note p {
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    line-height: 1.5;
  }

  .dialog-copy {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.75rem;
  }

  .dialog-copy p,
  .restore-reading p,
  .restore-reading strong {
    margin: 0;
  }

  .dialog-copy p {
    color: var(--color-ink-muted);
    font-size: 0.74rem;
    line-height: 1.55;
  }

  .restore-reading {
    display: grid;
    gap: 0.25rem;
    border: 1px solid var(--color-rule);
    margin-bottom: 1rem;
    padding: 0.8rem;
    background: var(--color-paper);
  }

  .restore-reading p {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .restore-reading strong {
    overflow-wrap: anywhere;
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.7rem;
  }

  .confirmation-field {
    display: grid;
    gap: 0.4rem;
    color: var(--color-ink-soft);
    font-size: 0.68rem;
    font-weight: 700;
  }

  .confirmation-field input {
    width: 100%;
    border: 1px solid var(--color-rule);
    padding: 0.72rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font-family: ui-monospace, "Cascadia Code", monospace;
  }

  @media (max-width: 68rem) {
    .continuity-workspace {
      grid-template-columns: 1fr;
    }

    .backup-ledger {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }
  }

  @media (max-width: 46rem) {
    .section-intro,
    .continuity-reading {
      grid-template-columns: 1fr;
    }

    .section-intro {
      gap: 0.75rem;
    }

    .continuity-reading article,
    .continuity-reading article:last-child {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .backup-list label {
      grid-template-columns: auto auto minmax(0, 1fr);
    }

    .backup-list em {
      grid-column: 3;
    }

    .backup-ledger footer,
    .restart-banner {
      align-items: stretch;
      grid-template-columns: 1fr;
      flex-direction: column;
    }
  }
</style>
