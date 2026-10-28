<script lang="ts">
  import type {
    FrontendAgentEventMessage,
    FrontendDeveloperChatMessage,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  type ActivityItem =
    | FrontendAgentEventMessage
    | FrontendDeveloperChatMessage;

  interface Props {
    items: ActivityItem[];
  }

  let { items }: Props = $props();
  let expanded = $state(false);

  const failedCount = $derived(
    items.filter(
      (item) =>
        item.role === "event" &&
        ["error", "failed", "critical"].includes(
          String(item.event.status ?? "").toLocaleLowerCase(),
        ),
    ).length,
  );
  const runningCount = $derived(
    items.filter(
      (item) =>
        item.role === "event" &&
        ["running", "started", "pending", "queued"].includes(
          String(item.event.status ?? "").toLocaleLowerCase(),
        ),
    ).length,
  );
  const names = $derived.by(() => {
    const values = items.map((item) => {
      if (item.role === "developer") return item.title;
      return String(
        item.event.payload?.tool_name ??
          item.event.title ??
          item.event.kind
            .split(".")
            .at(-1)
            ?.replaceAll("_", " ") ??
          "Activity",
      );
    });
    return [...new Set(values.filter(Boolean))].slice(0, 3);
  });
  const runtimeReferences = $derived.by(() => {
    const runIds = new Set<string>();
    const artifactIds = new Set<string>();
    for (const item of items) {
      if (item.role !== "event") continue;
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
      if (runId) runIds.add(runId);
      const rawArtifactIds =
        nested.artifact_ids ??
        payload.artifact_ids ??
        (item.event.subject?.type === "artifact"
          ? [item.event.subject.id]
          : []);
      if (Array.isArray(rawArtifactIds)) {
        rawArtifactIds.filter(Boolean).forEach((id) => artifactIds.add(String(id)));
      }
    }
    return {
      hasRun: runIds.size > 0,
      artifactIds: [...artifactIds].slice(0, 4),
    };
  });

  function titleFor(item: ActivityItem): string {
    if (item.role === "developer") return item.title;
    return String(
      item.event.title ??
        item.event.kind
          .split(".")
          .map((part) => part.replaceAll("_", " "))
          .join(" · "),
    );
  }

  function summaryFor(item: ActivityItem): string {
    if (item.role === "developer") return "Developer detail";
    return String(item.event.summary ?? item.event.status ?? "");
  }

  function detailFor(item: ActivityItem): string {
    if (item.role === "developer") {
      return JSON.stringify(item.developer, null, 2) ?? String(item.developer);
    }
    return (
      JSON.stringify(
        {
          event: item.event.kind,
          status: item.event.status ?? "informational",
          subject: item.event.subject,
          payload: item.event.payload ?? {},
        },
        null,
        2,
      ) ?? item.event.kind
    );
  }

  function openContext(tab: "runs" | "artifacts", artifactId?: string) {
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

<article
  class="activity-frame"
  class:activity-frame--failed={failedCount > 0}
  class:activity-frame--running={runningCount > 0}
>
  <button
    class="activity-frame__summary"
    type="button"
    aria-expanded={expanded}
    onclick={() => (expanded = !expanded)}
  >
    <span class="activity-frame__mark" aria-hidden="true">
      <Icon
        name={failedCount ? "close" : runningCount ? "activity" : "check"}
        size={13}
      />
    </span>
    <span class="activity-frame__copy">
      <strong>Tools & activity</strong>
      <small>
        {items.length} {items.length === 1 ? "update" : "updates"}
        {#if names.length} · {names.join(", ")}{/if}
      </small>
    </span>
    <span class="activity-frame__status">
      {failedCount
        ? `${failedCount} failed`
        : runningCount
          ? "Working"
          : "Complete"}
    </span>
    <Icon
      name={expanded ? "chevron-left" : "chevron-right"}
      size={12}
    />
  </button>

  {#if expanded}
    <div class="activity-frame__details">
      {#if runtimeReferences.hasRun || runtimeReferences.artifactIds.length}
        <div class="activity-frame__references">
          {#if runtimeReferences.hasRun}
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

      {#each items as item (item.id)}
        <details>
          <summary>
            <span>{titleFor(item)}</span>
            <small>{summaryFor(item)}</small>
          </summary>
          <pre>{detailFor(item)}</pre>
        </details>
      {/each}
    </div>
  {/if}
</article>

<style>
  .activity-frame {
    width: min(50rem, calc(100% - 1.25rem));
    margin: 0.12rem auto;
    border: 1px solid var(--color-rule);
    border-left: 2px solid var(--color-rule-strong);
    background: color-mix(
      in srgb,
      var(--color-surface-muted) 55%,
      transparent
    );
    color: var(--color-ink-soft);
  }

  .activity-frame--running {
    border-left-color: var(--color-lichen);
  }

  .activity-frame--failed {
    border-left-color: var(--color-danger);
  }

  .activity-frame__summary {
    width: 100%;
    min-height: 2.15rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 0.5rem;
    border: 0;
    padding: 0.3rem 0.55rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .activity-frame__summary:hover {
    background: color-mix(
      in srgb,
      var(--color-clay-muted) 55%,
      transparent
    );
  }

  .activity-frame__mark {
    width: 1.35rem;
    height: 1.35rem;
    display: grid;
    place-items: center;
    color: var(--color-lichen);
  }

  .activity-frame--failed .activity-frame__mark {
    color: var(--color-danger);
  }

  .activity-frame__copy {
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
  }

  .activity-frame__copy strong {
    flex: 0 0 auto;
    font-size: 0.64rem;
    font-weight: 680;
  }

  .activity-frame__copy small {
    min-width: 0;
    overflow: hidden;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .activity-frame__status {
    color: var(--color-ink-muted);
    font-size: 0.54rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .activity-frame__details {
    border-top: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .activity-frame__references {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.35rem 0.55rem;
  }

  .activity-frame__references button {
    min-height: 1.75rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.45rem;
    background: var(--color-surface);
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.58rem;
  }

  details {
    border-bottom: 1px solid var(--color-rule);
  }

  details:last-child {
    border-bottom: 0;
  }

  summary {
    min-height: 2rem;
    display: grid;
    grid-template-columns: minmax(9rem, 0.45fr) minmax(0, 1fr);
    align-items: center;
    gap: 0.7rem;
    padding: 0.3rem 0.65rem;
    cursor: pointer;
    font-size: 0.61rem;
  }

  summary:hover {
    background: var(--color-surface-muted);
  }

  summary small {
    overflow: hidden;
    color: var(--color-ink-muted);
    font-size: 0.57rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  pre {
    max-height: 22rem;
    overflow: auto;
    margin: 0;
    border-top: 1px solid var(--color-rule);
    padding: 0.55rem 0.7rem;
    background: var(--color-paper);
    color: var(--color-ink-soft);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.61rem;
    line-height: 1.4;
    white-space: pre-wrap;
  }

  @media (max-width: 42rem) {
    .activity-frame {
      width: calc(100% - 0.5rem);
    }

    .activity-frame__status {
      display: none;
    }

    summary {
      grid-template-columns: 1fr;
      gap: 0.08rem;
    }
  }
</style>
