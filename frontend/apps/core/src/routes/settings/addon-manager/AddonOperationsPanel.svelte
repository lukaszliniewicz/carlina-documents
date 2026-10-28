<script lang="ts">
  import type { FrontendAddonOperation } from "@carlina/sdk";
  import { Badge, Button, Icon } from "@carlina/ui";

  import {
    addonByteLabel,
    addonDateLabel,
    addonOperationActive,
    addonOperationLabel,
    addonOperationProgress,
    addonOperationStatusLabel,
    addonOperationTone,
    addonReadinessEntries,
    readableAddonLabel,
  } from "../../../lib/addon-manager.ts";

  interface Props {
    isAdmin: boolean;
    operations: readonly FrontendAddonOperation[];
    loading: boolean;
    error: string;
    actionPending: string;
    onrefresh: () => void;
    oncontrol: (
      operation: FrontendAddonOperation,
      action: "cancel" | "retry",
    ) => void;
    onnavigate: (href: string, event: MouseEvent) => void;
  }

  let {
    isAdmin,
    operations,
    loading,
    error,
    actionPending,
    onrefresh,
    oncontrol,
    onnavigate,
  }: Props = $props();

  const activeOperations = $derived(
    operations.filter((operation) => addonOperationActive(operation)),
  );
</script>

{#if isAdmin && (loading || error || operations.length > 0)}
  <section class="lifecycle-operations" aria-labelledby="lifecycle-operations-title">
    <header>
      <div>
        <span>Durable lifecycle queue</span>
        <h2 id="lifecycle-operations-title">Install and activation work</h2>
        <p>
          {activeOperations.length
            ? `${activeOperations.length} operation${activeOperations.length === 1 ? " is" : "s are"} active.`
            : "Recent work is retained here with progress and recovery details."}
        </p>
      </div>
      <button
        class="operation-refresh"
        disabled={loading}
        aria-label="Refresh lifecycle operations"
        onclick={onrefresh}
      >
        <Icon name="refresh" size={15} />
        {loading ? "Loading…" : "Refresh"}
      </button>
    </header>

    {#if error}
      <p class="operation-feedback" role="alert">{error}</p>
    {/if}

    {#if loading && operations.length === 0}
      <div class="operation-loading" aria-busy="true">
        <span></span><span></span><span></span>
      </div>
    {:else}
      <div class="operation-list">
        {#each operations.slice(0, 8) as operation (operation.id)}
          {@const progress = addonOperationProgress(operation)}
          {@const readiness = addonReadinessEntries(operation)}
          <article
            class="operation-card"
            class:operation-card--active={addonOperationActive(operation)}
            class:operation-card--failed={operation.status === "failed"}
          >
            <header>
              <div class="operation-title">
                <span>{addonOperationLabel(operation)} · attempt {operation.attempt}</span>
                <h3>{operation.addon_id ?? "Unknown addon"}</h3>
              </div>
              <Badge tone={addonOperationTone(operation)}>
                {addonOperationStatusLabel(operation)}
              </Badge>
            </header>

            <div
              class="operation-progress"
              role="progressbar"
              aria-label={`${operation.addon_id ?? "Addon"} lifecycle progress`}
              aria-valuemin="0"
              aria-valuemax="100"
              aria-valuenow={progress}
            >
              <span style:width={`${progress}%`}></span>
            </div>

            <div class="operation-stage">
              <strong>{readableAddonLabel(operation.stage)}</strong>
              <span>{progress.toFixed(0)}%</span>
              <small>Activity {addonDateLabel(operation.last_activity_at ?? operation.updated_at)}</small>
            </div>

            {#if operation.message}
              <p class="operation-message">{operation.message}</p>
            {/if}
            {#if operation.error}
              <p class="operation-error" role="alert">{operation.error}</p>
            {/if}

            {#if operation.bytes.current !== null || operation.bytes.total !== null}
              <p class="operation-transfer">
                <span>Transfer</span>
                <strong>
                  {addonByteLabel(operation.bytes.current)}
                  {#if operation.bytes.total !== null}
                    / {addonByteLabel(operation.bytes.total)}
                  {/if}
                </strong>
              </p>
            {/if}

            {#if operation.admission}
              <div class="operation-admission">
                <span>Admission</span>
                <strong>
                  {operation.admission.allowed ? "Passed" : "Blocked"}
                  · {addonByteLabel(operation.admission.costs.required_disk_bytes)} disk headroom
                </strong>
                {#each operation.admission.warnings.slice(0, 2) as warning}
                  <small>{warning}</small>
                {/each}
              </div>
            {/if}

            {#if readiness.length}
              <dl class="operation-readiness">
                {#each readiness as item}
                  <div>
                    <dt>{item.label}</dt>
                    <dd>{item.value}</dd>
                  </div>
                {/each}
              </dl>
            {/if}

            {#if operation.retry_expires_at && operation.input_retained !== false}
              <p class="operation-retention">
                Retained package expires {addonDateLabel(operation.retry_expires_at)}.
              </p>
            {/if}

            <footer>
              <nav aria-label={`${operation.addon_id ?? "Addon"} operation logs`}>
                {#each operation.log_links.slice(0, 3) as link}
                  <a href={link.url} onclick={(event) => onnavigate(link.url, event)}>
                    {link.name}
                  </a>
                {/each}
              </nav>
              <div class="operation-actions">
                {#if operation.cancellable}
                  <Button
                    variant="quiet"
                    disabled={Boolean(actionPending)}
                    onclick={() => oncontrol(operation, "cancel")}
                  >
                    {actionPending === `${operation.id}:cancel`
                      ? "Requesting…"
                      : "Cancel safely"}
                  </Button>
                {/if}
                {#if operation.retryable}
                  <Button
                    variant="outline"
                    disabled={Boolean(actionPending)}
                    onclick={() => oncontrol(operation, "retry")}
                  >
                    {actionPending === `${operation.id}:retry`
                      ? "Queuing…"
                      : "Retry"}
                  </Button>
                {/if}
              </div>
            </footer>
          </article>
        {/each}
      </div>
    {/if}
  </section>
{/if}
