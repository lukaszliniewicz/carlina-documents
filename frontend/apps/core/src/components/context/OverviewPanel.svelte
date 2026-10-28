<script lang="ts">
  import type {
    FrontendArtifactSummary,
    FrontendInspectorTab,
    FrontendWorkspaceSummary,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import {
    formatWorkspaceBytes,
    humanizeWorkspaceLabel,
  } from "../../lib/workspace-state.ts";

  interface Props {
    summary: FrontendWorkspaceSummary | null;
    loading: boolean;
    error: string;
    onselecttab: (tab: FrontendInspectorTab) => void;
    onopenartifact: (artifact: FrontendArtifactSummary) => void;
    onrefresh: () => void;
    onnavigate: (href: string, event: MouseEvent) => void;
  }

  let {
    summary,
    loading,
    error,
    onselecttab,
    onopenartifact,
    onrefresh,
    onnavigate,
  }: Props = $props();

  function statusLabel(value: string): string {
    return humanizeWorkspaceLabel(value);
  }

  function relativeTime(value: string | null): string {
    if (!value) return "No activity yet";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "Recently updated";
    const seconds = Math.round((date.valueOf() - Date.now()) / 1_000);
    const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
    const minutes = Math.round(seconds / 60);
    if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
    return formatter.format(Math.round(hours / 24), "day");
  }
</script>

<div
  id="context-panel-overview"
  class="overview"
  role="tabpanel"
  aria-labelledby="context-tab-overview"
>
  <header class="panel-heading">
    <div>
      <p>Right now</p>
      <h3>Workspace pulse</h3>
    </div>
    <span>{relativeTime(summary?.updated_at ?? null)}</span>
  </header>

  {#if loading && !summary}
    <div class="overview-loading" aria-label="Loading workspace" aria-busy="true">
      <span></span><span></span><span></span>
    </div>
  {:else if error && !summary}
    <div class="panel-error" role="alert">
      <Icon name="activity" size={16} />
      <p>{error}</p>
      <button type="button" onclick={onrefresh}>Try again</button>
    </div>
  {:else if !summary}
    <div class="empty-workspace">
      <span><Icon name="workspaces" size={20} /></span>
      <h4>Start a conversation first</h4>
      <p>Runs, working memory, and artifacts will gather here as you work.</p>
    </div>
  {:else}
    {#if summary.focus_run}
      <article class="focus-run">
        <header>
          <span class:status--active={summary.focus_run.active}>
            <i></i>{statusLabel(summary.focus_run.status)}
          </span>
          <small>
            {summary.focus_run.progress.completed} of
            {summary.focus_run.progress.total || "—"} steps
          </small>
        </header>
        <h4>{summary.focus_run.title}</h4>
        <p>
          {summary.focus_run.summary ||
            summary.focus_run.current_step_label ||
            "The run is ready to inspect."}
        </p>
        <div class="progress-label">
          <span>
            {summary.focus_run.current_step_label
              ? `Current · ${summary.focus_run.current_step_label}`
              : "Progress"}
          </span>
          <strong>{summary.focus_run.progress.percent}%</strong>
        </div>
        <div
          class="progress"
          role="progressbar"
          aria-label={`${summary.focus_run.title} progress`}
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={summary.focus_run.progress.percent}
        >
          <span style={`width:${summary.focus_run.progress.percent}%`}></span>
        </div>
        <footer>
          <button type="button" onclick={() => onselecttab("runs")}>
            View details
          </button>
          <a
            href={summary.focus_run.open_url}
            onclick={(event) =>
              onnavigate(summary.focus_run!.open_url, event)}
          >
            Open runtime <Icon name="arrow-up-right" size={13} />
          </a>
        </footer>
      </article>
    {:else}
      <article class="quiet-card">
        <span><Icon name="activity" size={17} /></span>
        <div>
          <h4>No runs in this conversation</h4>
          <p>Durable work will appear here without interrupting the chat.</p>
        </div>
      </article>
    {/if}

    {#each summary.attention.slice(0, 2) as attention (attention.id)}
      <button
        class="attention"
        type="button"
        onclick={() => onselecttab(attention.target_tab)}
      >
        <span aria-hidden="true">
          {attention.kind === "review" ? "R" : "?"}
        </span>
        <span>
          <small>Your input</small>
          <strong>{attention.title}</strong>
          <i>{attention.summary}</i>
        </span>
        <Icon name="chevron-right" size={14} />
      </button>
    {/each}

    <section class="mini-section">
      <header>
        <h4>Recent artifacts</h4>
        <button type="button" onclick={() => onselecttab("artifacts")}>
          View all
        </button>
      </header>
      {#if summary.recent_artifacts.length}
        {#each summary.recent_artifacts.slice(0, 3) as artifact (artifact.id)}
          <button
            class="artifact-row"
            type="button"
            onclick={() => onopenartifact(artifact)}
          >
            <span aria-hidden="true">
              {artifact.preview_kind === "markdown"
                ? "MD"
                : artifact.preview_kind === "structured"
                  ? "DATA"
                  : artifact.type.slice(0, 4).toUpperCase()}
            </span>
            <span>
              <strong>{artifact.label}</strong>
              <small>
                {humanizeWorkspaceLabel(artifact.preview_kind)} ·
                {humanizeWorkspaceLabel(artifact.review_status)} ·
                {formatWorkspaceBytes(artifact.size_bytes)}
              </small>
            </span>
            <Icon name="chevron-right" size={13} />
          </button>
        {/each}
      {:else}
        <p class="mini-empty">No artifacts have been created yet.</p>
      {/if}
    </section>

    <button
      class="memory-glance"
      type="button"
      onclick={() => onselecttab("memory")}
    >
      <span>
        <strong>
          {summary.counts.session_memory + summary.counts.personal_memory}
        </strong>
        <span>
          <b>Working memories</b>
          <small>
            {summary.counts.session_memory} session ·
            {summary.counts.personal_memory} personal
          </small>
        </span>
      </span>
      <span>Review memory <Icon name="arrow-right" size={13} /></span>
    </button>
  {/if}
</div>

<style>
  .overview {
    display: grid;
    align-content: start;
    gap: 0.8rem;
    padding: 1.2rem 1rem 3rem;
  }

  .panel-heading,
  .mini-section > header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .panel-heading {
    margin-bottom: 0.15rem;
  }

  .panel-heading p,
  .attention small {
    margin: 0 0 0.15rem;
    color: var(--color-clay);
    font-size: 0.54rem;
    font-style: normal;
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

  .panel-heading > span {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .focus-run {
    border: 1px solid var(--color-rule-strong);
    padding: 0.9rem;
    background: var(--color-surface);
  }

  .focus-run > header,
  .focus-run > footer,
  .progress-label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
  }

  .focus-run > header > span {
    display: inline-flex;
    align-items: center;
    gap: 0.38rem;
    border: 1px solid var(--color-rule);
    padding: 0.28rem 0.45rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 700;
  }

  .focus-run > header > span i {
    width: 0.32rem;
    height: 0.32rem;
    background: var(--color-ink-muted);
  }

  .focus-run > header > span.status--active {
    border-color: color-mix(in srgb, var(--color-lichen) 36%, var(--color-rule));
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .focus-run > header > span.status--active i {
    background: var(--color-lichen);
  }

  .focus-run > header small {
    color: var(--color-ink-muted);
    font-size: 0.57rem;
  }

  .focus-run h4,
  .quiet-card h4 {
    margin: 0.8rem 0 0.25rem;
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
  }

  .focus-run > p,
  .quiet-card p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
    line-height: 1.55;
  }

  .progress-label {
    margin: 0.9rem 0 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.57rem;
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

  .focus-run > footer {
    margin-top: 0.85rem;
  }

  .focus-run > footer button,
  .mini-section > header button {
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.6rem;
    font-weight: 700;
  }

  .focus-run > footer a {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--color-ink-soft);
    padding: 0 0.55rem;
    background: var(--color-ink-soft);
    color: var(--color-paper);
    font-size: 0.58rem;
    font-weight: 700;
    text-decoration: none;
  }

  .quiet-card {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.7rem;
    border: 1px solid var(--color-rule);
    padding: 0.85rem;
    background: var(--color-surface);
  }

  .quiet-card > span,
  .empty-workspace > span {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
  }

  .quiet-card h4 {
    margin-top: 0;
  }

  .attention {
    width: 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    border: 1px solid color-mix(in srgb, var(--color-tobacco) 38%, var(--color-rule));
    padding: 0.65rem;
    background: color-mix(in srgb, var(--color-tobacco-muted) 58%, var(--color-surface));
    color: var(--color-ink-soft);
    cursor: pointer;
    text-align: left;
  }

  .attention > span:first-child {
    width: 1.8rem;
    height: 1.8rem;
    display: grid;
    place-items: center;
    background: var(--color-tobacco);
    color: var(--color-surface);
    font-family: var(--font-editorial);
    font-size: 0.75rem;
    font-weight: 750;
  }

  .attention > span:nth-child(2) {
    min-width: 0;
    display: grid;
  }

  .attention strong {
    font-family: var(--font-editorial);
    font-size: 0.78rem;
    font-weight: 600;
  }

  .attention i {
    color: var(--color-ink-muted);
    font-size: 0.57rem;
    font-style: normal;
  }

  .mini-section {
    margin-top: 0.65rem;
  }

  .mini-section > header {
    align-items: center;
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 0.4rem;
  }

  .mini-section h4 {
    margin: 0;
    font-size: 0.66rem;
    font-weight: 720;
  }

  .artifact-row {
    width: 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.7rem 0.1rem;
    background: transparent;
    color: var(--color-ink-soft);
    cursor: pointer;
    text-align: left;
  }

  .artifact-row:hover strong {
    color: var(--color-clay);
  }

  .artifact-row > span:first-child {
    min-width: 2rem;
    height: 2.2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    background: var(--color-clay-muted);
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.48rem;
    font-weight: 750;
  }

  .artifact-row > span:nth-child(2) {
    min-width: 0;
    display: grid;
    gap: 0.12rem;
  }

  .artifact-row strong,
  .artifact-row small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .artifact-row strong {
    font-size: 0.65rem;
  }

  .artifact-row small,
  .mini-empty {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .mini-empty {
    margin: 0.8rem 0;
  }

  .memory-glance {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
    border: 1px dashed var(--color-rule-strong);
    margin-top: 0.65rem;
    padding: 0.8rem;
    background: transparent;
    color: var(--color-ink-soft);
    cursor: pointer;
    text-align: left;
  }

  .memory-glance > span {
    display: flex;
    align-items: center;
    gap: 0.65rem;
  }

  .memory-glance > span:first-child > strong {
    font-family: var(--font-editorial);
    font-size: 1.7rem;
    font-weight: 480;
  }

  .memory-glance > span:first-child > span {
    display: grid;
    gap: 0.12rem;
  }

  .memory-glance b {
    font-size: 0.64rem;
  }

  .memory-glance small {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .memory-glance > span:last-child {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 700;
  }

  .overview-loading {
    display: grid;
    gap: 0.7rem;
  }

  .overview-loading span {
    height: 7rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: loading 1.5s infinite linear;
  }

  .overview-loading span:nth-child(2) {
    height: 3.5rem;
  }

  .overview-loading span:nth-child(3) {
    height: 5rem;
  }

  .panel-error,
  .empty-workspace {
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
  .empty-workspace p {
    margin: 0;
    font-size: 0.66rem;
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

  .empty-workspace h4 {
    margin: 0.15rem 0 0;
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 550;
  }

  .empty-workspace p {
    color: var(--color-ink-muted);
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }
</style>
