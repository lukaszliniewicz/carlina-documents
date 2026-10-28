<script lang="ts">
  import type {
    FrontendArtifactSummary,
    FrontendRunStepDetail,
  } from "@carlina/sdk";
  import SchemaFields from "@carlina/runtime-ui/schema-fields";
  import StatusMark from "@carlina/runtime-ui/status-mark";
  import { Button, Icon } from "@carlina/ui";

  import { formatRunDate, formatRunDuration } from "../../lib/run-studio.ts";
  import StructuredData from "../context/StructuredData.svelte";

  interface Props {
    detail: FrontendRunStepDetail | null;
    loading: boolean;
    error: string;
    busy: boolean;
    onrerun: (detail: FrontendRunStepDetail) => void;
    onartifact: (artifact: FrontendArtifactSummary) => void;
    ondeveloper: () => void;
  }

  let {
    detail,
    loading,
    error,
    busy,
    onrerun,
    onartifact,
    ondeveloper,
  }: Props = $props();

  let section = $state<"summary" | "input" | "output" | "activity">("summary");

  $effect(() => {
    detail?.step.id;
    section = "summary";
  });

  function logLink(record: Readonly<Record<string, unknown>>): string | null {
    const candidate = record.href ?? record.url ?? record.path;
    return typeof candidate === "string" && candidate.startsWith("/")
      ? candidate
      : null;
  }

  function logLabel(record: Readonly<Record<string, unknown>>): string {
    const candidate = record.label ?? record.name ?? record.id;
    return typeof candidate === "string" ? candidate : "Open related log";
  }
</script>

<aside class="step-inspector" aria-label="Selected step inspector">
  {#if loading}
    <div class="loading" aria-busy="true">
      <span></span><span></span><span></span>
    </div>
  {:else if error}
    <div class="error" role="alert">
      <Icon name="activity" size={20} />
      <strong>Step unavailable</strong>
      <p>{error}</p>
    </div>
  {:else if detail}
    <header class="step-header">
      <div>
        <small>Selected step</small>
        <h2>{detail.step.title}</h2>
      </div>
      <StatusMark status={detail.step.status} label={detail.step.status_label} />
      <p>{detail.step.purpose}</p>
      <dl class="step-facts">
        <div>
          <dt>Role</dt>
          <dd>{detail.step.role || "Unassigned"}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{formatRunDuration(detail.step.duration_ms)}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{formatRunDate(detail.step.started_at)}</dd>
        </div>
      </dl>
    </header>

    <nav class="inspector-tabs" aria-label="Step details">
      {#each ["summary", "input", "output", "activity"] as tab}
        <button
          type="button"
          class:active={section === tab}
          aria-current={section === tab ? "page" : undefined}
          onclick={() => (section = tab as typeof section)}
        >
          {tab}
          {#if tab === "output" && detail.output.artifacts.length}
            <span>{detail.output.artifacts.length}</span>
          {:else if tab === "activity" && detail.activity.length}
            <span>{detail.activity.length}</span>
          {/if}
        </button>
      {/each}
    </nav>

    <div class="inspector-body">
      {#if section === "summary"}
        {#if detail.problems.has_problem}
          <section class="problem">
            <small>Problem</small>
            <strong>{detail.problems.summary}</strong>
            <p>{detail.problems.guidance}</p>
            {#if detail.problems.log_links.length}
              <nav aria-label="Related logs">
                {#each detail.problems.log_links as log}
                  {#if logLink(log)}
                    <a href={logLink(log)!}>{logLabel(log)}</a>
                  {/if}
                {/each}
              </nav>
            {/if}
          </section>
        {/if}

        <section class="inspector-section">
          <h3>Purpose and instructions</h3>
          {#if typeof detail.step.instructions === "string"}
            <p class="reading-copy">{detail.step.instructions || "No additional instructions."}</p>
          {:else}
            <StructuredData value={detail.step.instructions} maxRows={40} />
          {/if}
        </section>

        <section class="inspector-section">
          <h3>Assigned runtime</h3>
          <dl class="runtime-tags">
            <div>
              <dt>Capabilities</dt>
              <dd>{detail.step.capabilities.join(", ") || "None declared"}</dd>
            </div>
            <div>
              <dt>Skills</dt>
              <dd>{detail.step.skills.join(", ") || "None declared"}</dd>
            </div>
            <div>
              <dt>Tools</dt>
              <dd>{detail.step.tools.join(", ") || "None declared"}</dd>
            </div>
          </dl>
        </section>
      {:else if section === "input"}
        <section class="inspector-section">
          <header>
            <h3>Step inputs</h3>
            <p>{detail.input.summary}</p>
          </header>
          <SchemaFields fields={detail.input.fields} />
        </section>

        {#if detail.input.parent_outputs.length}
          <section class="inspector-section">
            <h3>Upstream outputs</h3>
            <div class="parent-outputs">
              {#each detail.input.parent_outputs as parent (parent.step_id)}
                <details>
                  <summary>
                    <strong>{parent.title}</strong>
                    <span>{parent.summary}</span>
                  </summary>
                  <StructuredData value={parent.value} maxRows={40} />
                </details>
              {/each}
            </div>
          </section>
        {/if}

        {#if detail.input.resources.length}
          <section class="inspector-section">
            <h3>Resources</h3>
            <ul class="resources">
              {#each detail.input.resources as resource (resource.id)}
                <li>
                  <strong>{resource.label}</strong>
                  <span>{resource.provider || resource.type}</span>
                  <small>{resource.permissions.join(", ")}</small>
                </li>
              {/each}
            </ul>
          </section>
        {/if}
      {:else if section === "output"}
        <section class="inspector-section">
          <header>
            <h3>Step output</h3>
            <p>{detail.output.summary}</p>
          </header>
          <SchemaFields
            fields={detail.output.fields}
            emptyText="This step has not recorded an output yet."
          />
        </section>

        {#if detail.output.artifacts.length}
          <section class="inspector-section">
            <h3>Created artifacts</h3>
            <div class="artifact-list">
              {#each detail.output.artifacts as artifact (artifact.id)}
                <button type="button" onclick={() => onartifact(artifact)}>
                  <Icon name="file" size={17} />
                  <span>
                    <strong>{artifact.label}</strong>
                    <small>{artifact.mime_type} · {artifact.review_status}</small>
                  </span>
                  <Icon name="arrow-right" size={14} />
                </button>
              {/each}
            </div>
          </section>
        {/if}
      {:else}
        <section class="inspector-section">
          <header>
            <h3>Runtime activity</h3>
            <p>Typed events and job progress for this step.</p>
          </header>
          {#if detail.activity.length}
            <ol class="activity">
              {#each detail.activity as event (event.id)}
                <li>
                  <span class="activity-mark" aria-hidden="true"></span>
                  <div>
                    <span>
                      <strong>{event.title}</strong>
                      {#if event.status}<small>{event.status.replace(/[_-]+/g, " ")}</small>{/if}
                    </span>
                    {#if event.summary}<p>{event.summary}</p>{/if}
                    {#if event.progress !== null}
                      <div
                        class="event-progress"
                        role="progressbar"
                        aria-valuenow={event.progress}
                        aria-valuemin="0"
                        aria-valuemax="100"
                      >
                        <i style={`width: ${event.progress}%`}></i>
                      </div>
                    {/if}
                    <time>{formatRunDate(event.occurred_at)}</time>
                  </div>
                </li>
              {/each}
            </ol>
          {:else}
            <p class="reading-copy">No activity events have been recorded yet.</p>
          {/if}
        </section>
      {/if}
    </div>

    <footer class="step-actions">
      {#if detail.step.can_rerun}
        <Button
          variant="outline"
          size="sm"
          onclick={() => onrerun(detail)}
          disabled={busy}
        >
          <Icon name="refresh" size={14} />
          {busy ? "Rerunning…" : "Rerun step"}
        </Button>
      {/if}
      <Button variant="quiet" size="sm" onclick={ondeveloper}>
        <Icon name="code" size={14} />
        Developer data
      </Button>
    </footer>
  {:else}
    <div class="empty">
      <Icon name="activity" size={23} />
      <strong>Select a step</strong>
      <p>Choose a graph node or list row to inspect its inputs, output, and activity.</p>
    </div>
  {/if}
</aside>

<style>
  .step-inspector {
    min-width: 0;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .step-header {
    display: grid;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem;
  }

  .step-header > div:first-child {
    display: grid;
    gap: 0.18rem;
  }

  .step-header small,
  .problem > small {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 780;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.35rem;
    font-weight: 530;
    line-height: 1.12;
  }

  .step-header > p {
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.68rem;
    line-height: 1.55;
  }

  .step-facts,
  .runtime-tags {
    display: grid;
    gap: 1px;
    margin: 0;
    background: var(--color-rule);
  }

  .step-facts {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .step-facts div,
  .runtime-tags div {
    min-width: 0;
    display: grid;
    gap: 0.2rem;
    padding: 0.5rem;
    background: var(--color-surface-muted);
  }

  dt {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
    text-transform: uppercase;
  }

  dd {
    overflow-wrap: anywhere;
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.62rem;
  }

  .inspector-tabs {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-bottom: 1px solid var(--color-rule);
  }

  .inspector-tabs button {
    min-width: 0;
    min-height: 2.65rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.32rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.59rem;
    font-weight: 700;
    text-transform: capitalize;
  }

  .inspector-tabs button:last-child {
    border-right: 0;
  }

  .inspector-tabs button.active {
    box-shadow: inset 0 -2px var(--color-clay);
    color: var(--color-ink);
  }

  .inspector-tabs span {
    min-width: 1rem;
    padding: 0.08rem 0.22rem;
    background: var(--color-surface-muted);
    font-size: 0.53rem;
  }

  .inspector-body {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .inspector-section,
  .problem {
    min-width: 0;
    display: grid;
    gap: 0.7rem;
    padding: 0.95rem 1rem;
    background: var(--color-surface);
  }

  .inspector-section > header {
    display: grid;
    gap: 0.2rem;
  }

  h3 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 560;
  }

  .inspector-section header p,
  .reading-copy,
  .problem p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
    line-height: 1.55;
  }

  .problem {
    border-left: 3px solid var(--color-danger);
    background: var(--color-danger-muted);
  }

  .problem > strong {
    color: var(--color-danger);
    font-size: 0.72rem;
  }

  .problem nav {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .problem a {
    color: var(--color-danger);
    font-size: 0.62rem;
    font-weight: 700;
    text-underline-offset: 0.2rem;
  }

  .parent-outputs,
  .artifact-list {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .parent-outputs details {
    padding: 0.65rem;
    background: var(--color-surface-muted);
  }

  .parent-outputs summary {
    display: grid;
    gap: 0.15rem;
    cursor: pointer;
  }

  .parent-outputs summary strong {
    font-size: 0.67rem;
  }

  .parent-outputs summary span {
    color: var(--color-ink-muted);
    font-size: 0.6rem;
  }

  .parent-outputs details > :global(*) {
    margin-top: 0.6rem;
  }

  .resources {
    display: grid;
    gap: 1px;
    margin: 0;
    padding: 0;
    background: var(--color-rule);
    list-style: none;
  }

  .resources li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.16rem 0.5rem;
    padding: 0.6rem;
    background: var(--color-surface-muted);
  }

  .resources strong {
    font-size: 0.67rem;
  }

  .resources span,
  .resources small {
    color: var(--color-ink-muted);
    font-size: 0.57rem;
  }

  .resources small {
    grid-column: 1 / -1;
  }

  .artifact-list button {
    min-height: 3.3rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.6rem;
    border: 0;
    padding: 0.55rem 0.65rem;
    background: var(--color-surface-muted);
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .artifact-list button:hover {
    background: var(--color-clay-muted);
  }

  .artifact-list button > span {
    min-width: 0;
    display: grid;
    gap: 0.16rem;
  }

  .artifact-list strong,
  .artifact-list small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .artifact-list strong {
    font-size: 0.65rem;
  }

  .artifact-list small {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .activity {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .activity li {
    display: grid;
    grid-template-columns: 1rem minmax(0, 1fr);
    gap: 0.55rem;
  }

  .activity-mark {
    position: relative;
    width: 0.5rem;
    height: 0.5rem;
    border: 1px solid var(--color-clay);
    margin-top: 0.35rem;
    background: var(--color-surface);
  }

  .activity li:not(:last-child) .activity-mark::after {
    position: absolute;
    top: 0.48rem;
    left: 0.18rem;
    width: 1px;
    height: calc(100% + 3.2rem);
    background: var(--color-rule);
    content: "";
  }

  .activity li > div {
    min-width: 0;
    display: grid;
    gap: 0.25rem;
    padding-bottom: 1rem;
  }

  .activity li > div > span {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .activity strong {
    font-size: 0.66rem;
  }

  .activity small,
  .activity time {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .activity p {
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.62rem;
    line-height: 1.5;
  }

  .event-progress {
    height: 2px;
    background: var(--color-surface-sunken);
  }

  .event-progress i {
    display: block;
    height: 100%;
    background: var(--color-lichen);
  }

  .step-actions {
    display: flex;
    justify-content: space-between;
    gap: 0.45rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.75rem 1rem;
  }

  .loading {
    display: grid;
    gap: 0.65rem;
    padding: 1rem;
  }

  .loading span {
    height: 3.6rem;
    background: var(--color-surface-muted);
  }

  .error,
  .empty {
    min-height: 18rem;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.55rem;
    padding: 1rem;
  }

  .error strong,
  .empty strong {
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
  }

  .error p,
  .empty p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.67rem;
    line-height: 1.5;
  }

  @media (max-width: 34rem) {
    .step-facts {
      grid-template-columns: 1fr;
    }

    .step-actions {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
