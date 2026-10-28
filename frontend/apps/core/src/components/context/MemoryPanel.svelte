<script lang="ts">
  import { onDestroy } from "svelte";
  import type { FrontendMemoryEntry } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import { humanizeWorkspaceLabel } from "../../lib/workspace-state.ts";
  import StructuredData from "./StructuredData.svelte";

  interface Props {
    items: FrontendMemoryEntry[];
    total: number;
    nextCursor: string | null;
    loading: boolean;
    loadingMore: boolean;
    error: string;
    scope: "all" | "session" | "personal";
    query: string;
    onchangescope: (scope: "all" | "session" | "personal") => void;
    onquery: (query: string) => void;
    onloadmore: () => void;
    onrefresh: () => void;
  }

  let {
    items,
    total,
    nextCursor,
    loading,
    loadingMore,
    error,
    scope,
    query,
    onchangescope,
    onquery,
    onloadmore,
    onrefresh,
  }: Props = $props();

  let localQuery = $state("");
  let searchTimer: number | null = null;

  $effect(() => {
    if (query !== localQuery && searchTimer === null) localQuery = query;
  });

  onDestroy(() => {
    if (searchTimer !== null) window.clearTimeout(searchTimer);
  });

  function changeQuery(value: string) {
    localQuery = value;
    if (searchTimer !== null) window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      searchTimer = null;
      onquery(localQuery);
    }, 280);
  }

  function dateLabel(value: string | null): string {
    if (!value) return "No timestamp";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "Recently updated";
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year:
        date.getFullYear() === new Date().getFullYear()
          ? undefined
          : "numeric",
    }).format(date);
  }
</script>

<div
  id="context-panel-memory"
  class="memory"
  role="tabpanel"
  aria-labelledby="context-tab-memory"
>
  <header class="panel-heading">
    <div>
      <p>Human-readable by default</p>
      <h3>Memory</h3>
    </div>
    <a href="/settings/policy">
      Policy <Icon name="arrow-up-right" size={13} />
    </a>
  </header>

  <label class="search-field">
    <Icon name="search" size={14} />
    <span class="sr-only">Search workspace memory</span>
    <input
      type="search"
      value={localQuery}
      placeholder="Search this workspace"
      oninput={(event) =>
        changeQuery((event.currentTarget as HTMLInputElement).value)}
    />
    {#if localQuery}
      <button type="button" aria-label="Clear search" onclick={() => changeQuery("")}>
        <Icon name="close" size={12} />
      </button>
    {/if}
  </label>

  <div class="scope-filter" role="group" aria-label="Memory scope">
    {#each ["all", "session", "personal"] as option}
      <button
        type="button"
        class:is-active={scope === option}
        aria-pressed={scope === option}
        onclick={() =>
          onchangescope(option as "all" | "session" | "personal")}
      >
        {humanizeWorkspaceLabel(option)}
      </button>
    {/each}
  </div>

  <p class="result-count">
    {total.toLocaleString()} {total === 1 ? "memory" : "memories"}
    {query ? ` matching “${query}”` : ""}
  </p>

  {#if loading && !items.length}
    <div class="memory-loading" aria-busy="true" aria-label="Loading memory">
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
      <span><Icon name="brain" size={19} /></span>
      <h4>{query ? "No matching memory" : "Nothing recorded yet"}</h4>
      <p>
        {query
          ? "Try a broader phrase or another memory scope."
          : "Session notes and relevant personal preferences will appear here as readable cards."}
      </p>
    </div>
  {:else}
    <div class="memory-list">
      {#each items as memory (memory.id)}
        <article>
          <header>
            <span class:scope--personal={memory.scope === "personal"}>
              {humanizeWorkspaceLabel(memory.scope)}
            </span>
            <time datetime={memory.updated_at ?? undefined}>
              {dateLabel(memory.updated_at)}
            </time>
          </header>
          <h4>{memory.title}</h4>
          {#if memory.value_kind === "structured" || memory.value_kind === "list"}
            <p class="memory-summary">{memory.summary}</p>
            <StructuredData value={memory.value} compact maxRows={30} />
          {:else}
            <p class="memory-value">{String(memory.value ?? "Not set")}</p>
          {/if}
          <footer>
            <span>{humanizeWorkspaceLabel(memory.category)}</span>
            <span>
              {humanizeWorkspaceLabel(memory.importance)} importance
              {#if memory.tier}
                · {memory.tier === "tier1" ? "Active" : "Archive"}
              {/if}
            </span>
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
        {loadingMore ? "Loading more memory…" : "Load more memory"}
      </button>
    {/if}
  {/if}
</div>

<style>
  .memory {
    display: grid;
    align-content: start;
    gap: 0.75rem;
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

  .search-field {
    min-height: 2.45rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.6rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
  }

  .search-field:focus-within {
    border-color: var(--color-focus);
    box-shadow: 0 0 0 2px var(--color-focus-muted);
  }

  .search-field input {
    width: 100%;
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    color: var(--color-ink);
    font-size: 0.65rem;
  }

  .search-field button {
    width: 1.6rem;
    height: 1.6rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border: 0;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
  }

  .scope-filter {
    display: flex;
    gap: 1px;
    background: var(--color-rule);
  }

  .scope-filter button {
    min-height: 2rem;
    flex: 1;
    border: 0;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.57rem;
  }

  .scope-filter button.is-active {
    background: var(--color-clay-muted);
    color: var(--color-clay);
    font-weight: 750;
  }

  .result-count {
    margin: -0.15rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.54rem;
  }

  .memory-list {
    display: grid;
    gap: 0.65rem;
  }

  .memory-list article {
    border: 1px solid var(--color-rule);
    padding: 0.8rem;
    background: var(--color-surface);
    content-visibility: auto;
    contain-intrinsic-size: auto 12rem;
  }

  .memory-list article > header,
  .memory-list article > footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.55rem;
  }

  .memory-list article > header > span {
    border: 1px solid var(--color-rule);
    padding: 0.22rem 0.38rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.5rem;
    font-weight: 750;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .memory-list article > header > span.scope--personal {
    background: var(--color-clay-muted);
    color: var(--color-clay);
  }

  time {
    color: var(--color-ink-muted);
    font-size: 0.52rem;
  }

  .memory-list h4 {
    margin: 0.65rem 0 0.28rem;
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 560;
  }

  .memory-value,
  .memory-summary {
    overflow-wrap: anywhere;
    margin: 0 0 0.7rem;
    color: var(--color-ink-soft);
    font-family: var(--font-editorial);
    font-size: 0.74rem;
    line-height: 1.58;
    white-space: pre-wrap;
  }

  .memory-summary {
    color: var(--color-ink-muted);
    font-family: var(--font-sans);
    font-size: 0.61rem;
  }

  .memory-list article > footer {
    margin-top: 0.7rem;
    border-top: 1px solid var(--color-rule);
    padding-top: 0.55rem;
    color: var(--color-ink-muted);
    font-size: 0.51rem;
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

  .memory-loading {
    display: grid;
    gap: 0.65rem;
  }

  .memory-loading span {
    height: 9rem;
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

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }
</style>
