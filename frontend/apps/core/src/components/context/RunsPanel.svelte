<script lang="ts">
  import type { FrontendRunSummary } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import { humanizeWorkspaceLabel } from "../../lib/workspace-state.ts";

  interface Props {
    items: FrontendRunSummary[];
    total: number;
    nextCursor: string | null;
    loading: boolean;
    loadingMore: boolean;
    error: string;
    view: "all" | "active" | "attention" | "completed";
    onchangeview: (
      view: "all" | "active" | "attention" | "completed",
    ) => void;
    onloadmore: () => void;
    onrefresh: () => void;
    onnavigate: (href: string, event: MouseEvent) => void;
  }

  let {
    items,
    total,
    nextCursor,
    loading,
    loadingMore,
    error,
    view,
    onchangeview,
    onloadmore,
    onrefresh,
    onnavigate,
  }: Props = $props();

  const views = [
    { id: "all", label: "All" },
    { id: "active", label: "Active" },
    { id: "attention", label: "Needs input" },
    { id: "completed", label: "Finished" },
  ] as const;

  function dateLabel(value: string | null): string {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "";
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(date);
  }
</script>

<div
  id="context-panel-runs"
  class="runs"
  role="tabpanel"
  aria-labelledby="context-tab-runs"
>
  <header class="panel-heading">
    <div>
      <p>{total} in this conversation</p>
      <h3>Runs</h3>
    </div>
    <a href="/runs" onclick={(event) => onnavigate("/runs", event)}>
      All runtime <Icon name="arrow-up-right" size={13} />
    </a>
  </header>

  <div class="run-filters" role="group" aria-label="Filter conversation runs">
    {#each views as option (option.id)}
      <button
        type="button"
        class:is-active={view === option.id}
        aria-pressed={view === option.id}
        onclick={() => onchangeview(option.id)}
      >
        {option.label}
      </button>
    {/each}
  </div>

  {#if loading && !items.length}
    <div class="run-loading" aria-busy="true" aria-label="Loading runs">
      <span></span><span></span><span></span>
    </div>
  {:else if error && !items.length}
    <div class="panel-error" role="alert">
      <Icon name="activity" size={16} />
      <p>{error}</p>
      <button type="button" onclick={onrefresh}>Try again</button>
    </div>
  {:else if !items.length}
    <div class="empty">
      <span><Icon name="activity" size={19} /></span>
      <h4>No matching runs</h4>
      <p>
        {view === "all"
          ? "Ask for durable work in chat and its progress will appear here."
          : "Choose another filter to review the conversation's run history."}
      </p>
    </div>
  {:else}
    <div class="run-list">
      {#each items as run (run.id)}
        <article class:run--attention={run.needs_attention}>
          <header>
            <span class:status--active={run.active}>
              <i></i>{humanizeWorkspaceLabel(run.status)}
            </span>
            <time datetime={run.updated_at ?? undefined}>
              {dateLabel(run.updated_at)}
            </time>
          </header>
          <h4>{run.title}</h4>
          <p>
            {run.summary ||
              run.current_step_label ||
              (run.active ? "Work is in progress." : "Run details are ready.")}
          </p>
          {#if run.current_step_label}
            <div class="current-step">
              <span>Current step</span>
              <strong>{run.current_step_label}</strong>
            </div>
          {/if}
          <div class="progress-label">
            <span>
              {run.progress.completed} of {run.progress.total || "—"} steps
            </span>
            <strong>{run.progress.percent}%</strong>
          </div>
          <div
            class="progress"
            role="progressbar"
            aria-label={`${run.title} progress`}
            aria-valuemin="0"
            aria-valuemax="100"
            aria-valuenow={run.progress.percent}
          >
            <span style={`width:${run.progress.percent}%`}></span>
          </div>
          <footer>
            <span>
              {#if run.pending_approval_count}
                <b>{run.pending_approval_count} approval waiting</b>
              {/if}
              {#if run.artifact_count}
                <i>
                  {run.artifact_count}
                  {run.artifact_count === 1 ? "artifact" : "artifacts"}
                </i>
              {/if}
            </span>
            <a
              href={run.open_url}
              onclick={(event) => onnavigate(run.open_url, event)}
            >
              Preview <Icon name="arrow-up-right" size={13} />
            </a>
          </footer>
        </article>
      {/each}
    </div>

    {#if nextCursor}
      <button
        class="load-more"
        type="button"
        disabled={loadingMore}
        onclick={onloadmore}
      >
        {loadingMore ? "Loading more runs…" : "Load more runs"}
      </button>
    {/if}
  {/if}
</div>

<style>
  .runs {
    display: grid;
    align-content: start;
    gap: 0.8rem;
    padding: 1.2rem 1rem 3rem;
  }

  .panel-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .panel-heading p {
    margin: 0 0 0.15rem;
    color: var(--color-clay);
    font-size: 0.54rem;
    font-weight: 750;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .panel-heading h3 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.22rem;
    font-weight: 540;
  }

  .panel-heading a {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.55rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    text-decoration: none;
  }

  .run-filters {
    display: flex;
    gap: 1px;
    background: var(--color-rule);
  }

  .run-filters button {
    min-height: 2rem;
    flex: 1;
    border: 0;
    padding: 0 0.35rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.57rem;
  }

  .run-filters button.is-active {
    background: var(--color-clay-muted);
    color: var(--color-clay);
    font-weight: 750;
  }

  .run-list {
    display: grid;
    gap: 0.65rem;
  }

  .run-list article {
    border: 1px solid var(--color-rule);
    border-left: 2px solid var(--color-rule-strong);
    padding: 0.8rem;
    background: var(--color-surface);
    content-visibility: auto;
    contain-intrinsic-size: auto 13rem;
  }

  .run-list article.run--attention {
    border-left-color: var(--color-tobacco);
    background: color-mix(
      in srgb,
      var(--color-tobacco-muted) 28%,
      var(--color-surface)
    );
  }

  .run-list article > header,
  .progress-label,
  .run-list article > footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.55rem;
  }

  .run-list article > header > span {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.55rem;
    font-weight: 720;
    text-transform: uppercase;
  }

  .run-list article > header > span i {
    width: 0.35rem;
    height: 0.35rem;
    background: var(--color-rule-strong);
  }

  .run-list article > header > span.status--active {
    color: var(--color-lichen);
  }

  .run-list article > header > span.status--active i {
    background: var(--color-lichen);
  }

  time {
    color: var(--color-ink-muted);
    font-size: 0.53rem;
  }

  .run-list h4 {
    margin: 0.65rem 0 0.2rem;
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 560;
  }

  .run-list article > p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    line-height: 1.5;
  }

  .current-step {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.5rem;
    margin-top: 0.7rem;
    border-top: 1px solid var(--color-rule);
    padding-top: 0.55rem;
    font-size: 0.57rem;
  }

  .current-step span {
    color: var(--color-ink-muted);
  }

  .current-step strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .progress-label {
    margin: 0.7rem 0 0.3rem;
    color: var(--color-ink-muted);
    font-size: 0.54rem;
  }

  .progress-label strong {
    color: var(--color-lichen);
  }

  .progress {
    height: 3px;
    background: var(--color-surface-sunken);
  }

  .progress span {
    height: 100%;
    display: block;
    background: var(--color-lichen);
  }

  .run-list article > footer {
    margin-top: 0.75rem;
  }

  .run-list article > footer > span {
    display: flex;
    gap: 0.45rem;
    font-size: 0.53rem;
  }

  .run-list article > footer b {
    color: var(--color-tobacco);
  }

  .run-list article > footer i {
    color: var(--color-ink-muted);
    font-style: normal;
  }

  .run-list article > footer a {
    min-height: 1.9rem;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0 0.5rem;
    color: var(--color-ink-soft);
    font-size: 0.56rem;
    font-weight: 700;
    text-decoration: none;
  }

  .load-more {
    min-height: 2.35rem;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.6rem;
  }

  .load-more:disabled {
    cursor: wait;
    opacity: 0.65;
  }

  .run-loading {
    display: grid;
    gap: 0.65rem;
  }

  .run-loading span {
    height: 10rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: loading 1.5s infinite linear;
  }

  .panel-error,
  .empty {
    display: grid;
    justify-items: start;
    gap: 0.6rem;
    border: 1px solid var(--color-rule);
    padding: 1rem;
    background: var(--color-surface);
  }

  .panel-error {
    grid-template-columns: auto minmax(0, 1fr);
    color: var(--color-danger);
  }

  .panel-error p,
  .empty p {
    margin: 0;
    font-size: 0.65rem;
    line-height: 1.5;
  }

  .panel-error button {
    grid-column: 2;
    border: 1px solid var(--color-rule);
    padding: 0.4rem 0.55rem;
    background: var(--color-surface);
    cursor: pointer;
    font-size: 0.6rem;
  }

  .empty > span {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
  }

  .empty h4 {
    margin: 0.15rem 0 0;
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 550;
  }

  .empty p {
    color: var(--color-ink-muted);
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }
</style>
