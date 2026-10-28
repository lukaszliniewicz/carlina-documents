<script lang="ts">
  import type { FrontendAgentEventMessage } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  interface Props {
    item: FrontendAgentEventMessage;
  }

  let { item }: Props = $props();
  let expanded = $state(false);

  const status = $derived(String(item.event.status ?? "informational"));
  const title = $derived(
    String(
      item.event.title ??
        item.event.kind
          .split(".")
          .map((part) => part.replaceAll("_", " "))
          .join(" · "),
    ),
  );
  const summary = $derived(String(item.event.summary ?? ""));
  const detailRows = $derived.by(() =>
    Object.entries(item.event.payload ?? {})
      .filter(([, value]) =>
        ["string", "number", "boolean"].includes(typeof value),
      )
      .slice(0, 8),
  );
  const progress = $derived.by(() => {
    const value = Number(item.event.payload?.progress);
    return Number.isFinite(value)
      ? Math.max(0, Math.min(100, value <= 1 ? value * 100 : value))
      : null;
  });
  const runtimeReferences = $derived.by(() => {
    const payload = item.event.payload ?? {};
    const nested =
      payload.runtime_references &&
      typeof payload.runtime_references === "object" &&
      !Array.isArray(payload.runtime_references)
        ? (payload.runtime_references as Readonly<Record<string, unknown>>)
        : {};
    const runId = String(
      nested.run_id ??
        nested.workflow_run_id ??
        payload.run_id ??
        payload.workflow_run_id ??
        "",
    );
    const rawArtifactIds =
      nested.artifact_ids ??
      payload.artifact_ids ??
      (item.event.subject?.type === "artifact"
        ? [item.event.subject.id]
        : []);
    return {
      runId,
      artifactIds: Array.isArray(rawArtifactIds)
        ? rawArtifactIds.filter(Boolean).map(String).slice(0, 4)
        : [],
    };
  });

  function openContext(
    tab: "runs" | "artifacts",
    artifactId?: string,
  ) {
    window.dispatchEvent(
      new CustomEvent("iarf:open-context", {
        detail: {
          tab,
          artifact_id: artifactId,
        },
      }),
    );
  }
</script>

<article class="event-card" class:event-card--active={status === "running"}>
  <span class="event-card__rail" aria-hidden="true"></span>
  <div class="event-card__body">
    <button
      class="event-card__summary"
      type="button"
      aria-expanded={expanded}
      onclick={() => (expanded = !expanded)}
    >
      <span class="event-card__mark" aria-hidden="true">
        {#if status === "completed" || status === "succeeded"}
          <Icon name="check" size={14} />
        {:else if status === "failed" || status === "error"}
          <Icon name="close" size={14} />
        {:else}
          <Icon name="activity" size={14} />
        {/if}
      </span>
      <span class="event-card__copy">
        <strong>{title}</strong>
        {#if summary}<small>{summary}</small>{/if}
      </span>
      <span class="event-card__status">{status}</span>
      <Icon
        name={expanded ? "chevron-left" : "chevron-right"}
        size={13}
      />
    </button>

    {#if progress !== null}
      <div
        class="event-card__progress"
        role="progressbar"
        aria-label={title}
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={Math.round(progress)}
      >
        <span style={`width: ${progress}%`}></span>
      </div>
    {/if}

    {#if runtimeReferences.runId || runtimeReferences.artifactIds.length}
      <div class="event-card__references" aria-label="Related workspace resources">
        {#if runtimeReferences.runId}
          <button type="button" onclick={() => openContext("runs")}>
            <Icon name="activity" size={12} /> Open run
          </button>
        {/if}
        {#each runtimeReferences.artifactIds as artifactId, index (artifactId)}
          <button
            type="button"
            onclick={() => openContext("artifacts", artifactId)}
          >
            <Icon name="file" size={12} />
            {runtimeReferences.artifactIds.length === 1
              ? "Open artifact"
              : `Artifact ${index + 1}`}
          </button>
        {/each}
      </div>
    {/if}

    {#if expanded}
      <div class="event-card__details">
        <dl>
          <div>
            <dt>Event</dt>
            <dd>{item.event.kind}</dd>
          </div>
          {#each detailRows as [label, value] (label)}
            <div>
              <dt>{label.replaceAll("_", " ")}</dt>
              <dd>{String(value)}</dd>
            </div>
          {/each}
        </dl>
      </div>
    {/if}
  </div>
</article>

<style>
  .event-card {
    position: relative;
    width: min(43rem, calc(100% - 2rem));
    display: grid;
    grid-template-columns: 1px minmax(0, 1fr);
    margin: 0.35rem auto;
    color: var(--color-ink-soft);
  }

  .event-card__rail {
    background: var(--color-rule-strong);
  }

  .event-card--active .event-card__rail {
    background: var(--color-lichen);
  }

  .event-card__body {
    min-width: 0;
    border-block: 1px solid var(--color-rule);
    border-right: 1px solid var(--color-rule);
    background: color-mix(in srgb, var(--color-surface-muted) 52%, transparent);
  }

  .event-card__summary {
    width: 100%;
    min-height: 2.65rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 0.62rem;
    border: 0;
    padding: 0.48rem 0.7rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .event-card__summary:hover {
    background: color-mix(in srgb, var(--color-surface) 65%, transparent);
  }

  .event-card__mark {
    width: 1.6rem;
    height: 1.6rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-lichen);
  }

  .event-card__copy {
    min-width: 0;
    display: grid;
    gap: 0.12rem;
  }

  .event-card__copy strong {
    overflow: hidden;
    font-size: 0.72rem;
    font-weight: 650;
    text-overflow: ellipsis;
    text-transform: capitalize;
    white-space: nowrap;
  }

  .event-card__copy small {
    overflow: hidden;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .event-card__status {
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .event-card__progress {
    height: 2px;
    background: var(--color-surface-sunken);
  }

  .event-card__progress span {
    height: 100%;
    display: block;
    background: var(--color-lichen);
    transition: width 180ms ease;
  }

  .event-card__details {
    border-top: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem 0.75rem 2.95rem;
  }

  .event-card__references {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.45rem 0.7rem 0.5rem 2.95rem;
  }

  .event-card__references button {
    min-height: 1.8rem;
    display: inline-flex;
    align-items: center;
    gap: 0.32rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.45rem;
    background: var(--color-surface);
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.55rem;
    font-weight: 700;
  }

  dl {
    display: grid;
    gap: 0.3rem;
    margin: 0;
  }

  dl div {
    display: grid;
    grid-template-columns: minmax(5rem, 0.35fr) minmax(0, 1fr);
    gap: 0.7rem;
  }

  dt {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    text-transform: capitalize;
  }

  dd {
    overflow-wrap: anywhere;
    margin: 0;
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.62rem;
  }

  @media (max-width: 42rem) {
    .event-card {
      width: calc(100% - 1rem);
    }

    .event-card__status {
      display: none;
    }
  }
</style>
