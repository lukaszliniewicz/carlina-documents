<script lang="ts">
  import { onDestroy } from "svelte";
  import type {
    FrontendContextDetail,
    FrontendContextItem,
    FrontendContextLifecycleState,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import { humanizeWorkspaceLabel } from "../../lib/workspace-state.ts";

  type ContextStateFilter = "all" | FrontendContextLifecycleState;

  interface Props {
    items: FrontendContextItem[];
    total: number;
    nextCursor: string | null;
    loading: boolean;
    loadingMore: boolean;
    error: string;
    lifecycleState: ContextStateFilter;
    query: string;
    searchTruncated: boolean;
    detail: FrontendContextDetail | null;
    detailLoading: boolean;
    detailLoadingMore: boolean;
    detailError: string;
    updatingId: string | null;
    onchangestate: (state: ContextStateFilter) => void;
    onquery: (query: string) => void;
    onloadmore: () => void;
    onrefresh: () => void;
    onopen: (item: FrontendContextItem) => void;
    onclosedetail: () => void;
    onloaddetailmore: () => void;
    onaction: (item: FrontendContextItem, action: string) => void;
  }

  let {
    items,
    total,
    nextCursor,
    loading,
    loadingMore,
    error,
    lifecycleState,
    query,
    searchTruncated,
    detail,
    detailLoading,
    detailLoadingMore,
    detailError,
    updatingId,
    onchangestate,
    onquery,
    onloadmore,
    onrefresh,
    onopen,
    onclosedetail,
    onloaddetailmore,
    onaction,
  }: Props = $props();

  const filters = [
    { id: "all", label: "All" },
    { id: "active", label: "Active" },
    { id: "archived", label: "Archive" },
    { id: "excluded", label: "Excluded" },
  ] as const;

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

  function sizeLabel(value: number): string {
    if (!Number.isFinite(value) || value <= 0) return "Empty";
    if (value < 1_000) return `${value} chars`;
    if (value < 1_000_000) return `${(value / 1_000).toFixed(1)}k chars`;
    return `${(value / 1_000_000).toFixed(1)}m chars`;
  }

  function tokenLabel(value: number | null): string {
    if (value === null || !Number.isFinite(value)) return "tokens unknown";
    if (value < 1_000) return `~${value} tokens`;
    if (value < 1_000_000) return `~${(value / 1_000).toFixed(1)}k tokens`;
    return `~${(value / 1_000_000).toFixed(1)}m tokens`;
  }

  function stateGuidance(item: FrontendContextItem): string {
    if (item.lifecycle_state === "archived") {
      return "Stored outside the live prompt; still searchable and exactly readable.";
    }
    if (item.lifecycle_state === "excluded") {
      return "Intentionally omitted from the live prompt; evidence remains retained.";
    }
    if (item.materialization === "full") {
      return "The exact body is projected into each provider call while active.";
    }
    if (item.materialization === "summary" || item.materialization === "excerpt") {
      return `A durable ${item.materialization} is projected; exact evidence remains available.`;
    }
    if (item.materialization === "overflow") {
      return "The original input exceeded admission and is retained for bounded reading.";
    }
    return "A compact reference is projected; exact evidence loads only on demand.";
  }
</script>

<div
  id="context-panel-context"
  class="context-items"
  role="tabpanel"
  aria-labelledby="context-tab-context"
>
  <header class="panel-heading">
    <div>
      <p>Direct prompt control</p>
      <h3>Context items</h3>
    </div>
    <button
      class="refresh"
      type="button"
      aria-label="Refresh context items"
      disabled={loading}
      onclick={onrefresh}
    >
      <Icon name="refresh" size={13} /> Refresh
    </button>
  </header>

  <p class="explanation">
    Full items enter the model payload. References and archived items keep stable
    IDs and exact evidence without repeatedly spending context.
  </p>

  <label class="search-field">
    <Icon name="search" size={14} />
    <span class="sr-only">Search context item titles and identifiers</span>
    <input
      type="search"
      value={localQuery}
      placeholder="Title, kind, or stable ID"
      oninput={(event) =>
        changeQuery((event.currentTarget as HTMLInputElement).value)}
    />
    {#if localQuery}
      <button type="button" aria-label="Clear search" onclick={() => changeQuery("")}>
        <Icon name="close" size={12} />
      </button>
    {/if}
  </label>

  <div class="state-filter" role="group" aria-label="Context lifecycle state">
    {#each filters as option (option.id)}
      <button
        type="button"
        class:is-active={lifecycleState === option.id}
        aria-pressed={lifecycleState === option.id}
        onclick={() => onchangestate(option.id)}
      >
        {option.label}
      </button>
    {/each}
  </div>

  <p class="result-count">
    {total.toLocaleString()} {total === 1 ? "item" : "items"}
    {query ? ` matching “${query}”` : ""}
    {#if searchTruncated} · first 5,000 searched{/if}
  </p>

  {#if detail || detailLoading || detailError}
    <section class="detail" aria-busy={detailLoading || detailLoadingMore}>
      <header>
        <div>
          <p>Exact evidence preview</p>
          <h4>{detail?.item.title ?? "Loading context item…"}</h4>
        </div>
        <button type="button" aria-label="Close evidence preview" onclick={onclosedetail}>
          <Icon name="close" size={13} />
        </button>
      </header>
      {#if detailError}
        <div class="detail-error" role="alert">{detailError}</div>
      {:else if detailLoading && !detail}
        <div class="detail-loading">Verifying and reading evidence…</div>
      {:else if detail}
        <div class="detail-manifest">
          <span>{detail.integrity_verified ? "Hash verified" : "Not verified"}</span>
          <span>{detail.start_offset.toLocaleString()}–{detail.end_offset.toLocaleString()}</span>
          <span>{sizeLabel(detail.evidence.char_count)}</span>
        </div>
        <pre>{detail.content || "This evidence slice is empty."}</pre>
        {#if detail.has_more}
          <button
            class="load-more"
            type="button"
            disabled={detailLoadingMore}
            onclick={onloaddetailmore}
          >
            {detailLoadingMore ? "Reading next slice…" : "Read next 20,000 characters"}
          </button>
        {/if}
      {/if}
    </section>
  {/if}

  {#if loading && !items.length}
    <div class="context-loading" aria-busy="true" aria-label="Loading context items">
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
      <span><Icon name="workspaces" size={19} /></span>
      <h4>{query ? "No matching context" : "No managed items yet"}</h4>
      <p>
        {query
          ? "Try a title, kind, evidence ID, or context item ID."
          : "Long inputs and substantial tool results become addressable here as the conversation continues."}
      </p>
    </div>
  {:else}
    <div class="context-list">
      {#each items as item (item.id)}
        <article
          class:item--archived={item.lifecycle_state === "archived"}
          class:item--excluded={item.lifecycle_state === "excluded"}
          class:item--pinned={item.is_pinned}
        >
          <details>
            <summary>
              <span class="item-state">
                <span class:state--active={item.lifecycle_state === "active"}>
                  {humanizeWorkspaceLabel(item.lifecycle_state)}
                </span>
                <span>{humanizeWorkspaceLabel(item.materialization)}</span>
              </span>
              <span class="item-summary">
                <strong>{item.title}</strong>
                <small>{sizeLabel(item.char_count)} · {tokenLabel(item.token_estimate)}</small>
              </span>
              <Icon name="chevron-right" size={14} />
            </summary>
            <div class="item-details">
              <p>{stateGuidance(item)}</p>
              <dl>
                <div>
                  <dt>Kind</dt>
                  <dd>{humanizeWorkspaceLabel(item.kind)}</dd>
                </div>
                <div>
                  <dt>Context ID</dt>
                  <dd title={item.id}>{item.id}</dd>
                </div>
                <div>
                  <dt>Evidence ID</dt>
                  <dd title={item.evidence_id}>{item.evidence_id}</dd>
                </div>
              </dl>
              <div class="actions">
                <button type="button" onclick={() => onopen(item)}>Preview</button>
                {#if item.lifecycle_state !== "active"}
                  <button
                    type="button"
                    disabled={updatingId === item.id}
                    onclick={() => onaction(item, "restore")}
                  >
                    Restore reference
                  </button>
                  <button
                    type="button"
                    disabled={updatingId === item.id}
                    onclick={() => onaction(item, "full")}
                  >
                    Load full
                  </button>
                {:else if item.materialization === "full"}
                  <button
                    type="button"
                    disabled={updatingId === item.id}
                    onclick={() => onaction(item, "reference")}
                  >
                    Use reference
                  </button>
                {:else}
                  <button
                    type="button"
                    disabled={updatingId === item.id}
                    onclick={() => onaction(item, "full")}
                  >
                    Load full
                  </button>
                {/if}
                {#if item.lifecycle_state === "active"}
                  <button
                    type="button"
                    disabled={item.is_pinned || updatingId === item.id}
                    title={item.is_pinned ? "Unpin before archiving" : "Move outside the live prompt"}
                    onclick={() => onaction(item, "archive")}
                  >
                    Archive
                  </button>
                  <button
                    type="button"
                    disabled={item.is_pinned || updatingId === item.id}
                    title={item.is_pinned ? "Unpin before excluding" : "Omit from the live prompt"}
                    onclick={() => onaction(item, "exclude")}
                  >
                    Exclude
                  </button>
                {/if}
                <button
                  type="button"
                  disabled={updatingId === item.id}
                  onclick={() => onaction(item, item.is_pinned ? "unpin" : "pin")}
                >
                  {item.is_pinned ? "Unpin" : "Pin"}
                </button>
              </div>
              {#if updatingId === item.id}
                <p class="saving">Saving audited transition…</p>
              {/if}
            </div>
          </details>
        </article>
      {/each}
    </div>

    {#if error}
      <p class="inline-error" role="alert">{error}</p>
    {/if}

    {#if nextCursor}
      <button
        class="load-more"
        type="button"
        disabled={loadingMore}
        onclick={onloadmore}
      >
        {loadingMore ? "Loading more context…" : "Load more context"}
      </button>
    {/if}
  {/if}
</div>

<style>
  .context-items {
    display: grid;
    align-content: start;
    gap: 0.75rem;
    padding: 1.2rem 1rem 3rem;
  }

  .panel-heading,
  .detail > header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .panel-heading p,
  .detail > header p {
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

  .refresh,
  .detail > header button {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.55rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.58rem;
  }

  .refresh:disabled {
    cursor: wait;
    opacity: 0.6;
  }

  .explanation {
    margin: 0;
    border-left: 2px solid var(--color-clay);
    padding: 0.5rem 0.65rem;
    background: var(--color-clay-muted);
    color: var(--color-ink-soft);
    font-size: 0.61rem;
    line-height: 1.5;
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

  .search-field button,
  .detail > header button {
    width: 1.8rem;
    height: 1.8rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
  }

  .state-filter {
    display: flex;
    gap: 1px;
    background: var(--color-rule);
  }

  .state-filter button {
    min-height: 2rem;
    flex: 1;
    border: 0;
    padding: 0 0.25rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.55rem;
  }

  .state-filter button.is-active {
    background: var(--color-clay-muted);
    color: var(--color-clay);
    font-weight: 750;
  }

  .result-count {
    margin: -0.15rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.54rem;
  }

  .detail {
    display: grid;
    gap: 0.65rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.8rem;
    background: var(--color-surface);
  }

  .detail h4 {
    overflow-wrap: anywhere;
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 0.94rem;
    font-weight: 560;
  }

  .detail-manifest {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .detail-manifest span {
    border: 1px solid var(--color-rule);
    padding: 0.2rem 0.35rem;
    color: var(--color-lichen);
    font-size: 0.49rem;
  }

  .detail pre {
    max-height: 25rem;
    overflow: auto;
    margin: 0;
    border: 1px solid var(--color-rule);
    padding: 0.7rem;
    background: var(--color-surface-sunken);
    color: var(--color-ink-soft);
    font-family: var(--font-mono);
    font-size: 0.58rem;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .detail-error,
  .detail-loading,
  .inline-error {
    margin: 0;
    color: var(--color-danger);
    font-size: 0.6rem;
    line-height: 1.45;
  }

  .detail-loading {
    color: var(--color-ink-muted);
  }

  .context-list {
    display: grid;
    gap: 0.65rem;
  }

  .context-list article {
    border: 1px solid var(--color-rule);
    border-left: 2px solid var(--color-lichen);
    background: var(--color-surface);
    content-visibility: auto;
    contain-intrinsic-size: auto 4.8rem;
  }

  .context-list article.item--archived {
    border-left-color: var(--color-clay);
  }

  .context-list article.item--excluded {
    border-left-color: var(--color-rule-strong);
    opacity: 0.88;
  }

  .context-list article.item--pinned {
    box-shadow: inset 0 0 0 1px var(--color-tobacco-muted);
  }

  .context-list details {
    min-width: 0;
  }

  .context-list summary {
    min-height: 3.55rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.55rem;
    padding: 0.55rem 0.65rem;
    cursor: pointer;
    list-style: none;
  }

  .context-list summary::-webkit-details-marker {
    display: none;
  }

  .context-list summary:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }

  .item-state {
    display: grid;
    gap: 0.18rem;
  }

  .item-state > span {
    width: max-content;
    max-width: 5.4rem;
    overflow: hidden;
    border: 1px solid var(--color-rule);
    padding: 0.2rem 0.35rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.48rem;
    font-weight: 730;
    letter-spacing: 0.04em;
    text-overflow: ellipsis;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .item-state > span.state--active {
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .item-summary {
    min-width: 0;
    display: grid;
    gap: 0.16rem;
  }

  .item-summary strong,
  .item-summary small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .item-summary strong {
    overflow-wrap: anywhere;
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 560;
  }

  .item-summary small {
    color: var(--color-ink-muted);
    font-size: 0.54rem;
  }

  .context-list summary > :global(svg) {
    transition: transform 150ms ease;
  }

  .context-list details[open] summary > :global(svg) {
    transform: rotate(90deg);
  }

  .item-details {
    display: grid;
    gap: 0.65rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.7rem 0.8rem 0.8rem;
  }

  .item-details > p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    line-height: 1.5;
  }

  .context-list dl {
    display: grid;
    gap: 0.35rem;
    margin: 0;
  }

  .context-list dl div {
    min-width: 0;
    display: grid;
    grid-template-columns: 4.2rem minmax(0, 1fr);
    gap: 0.4rem;
    font-size: 0.52rem;
  }

  .context-list dt {
    color: var(--color-ink-muted);
  }

  .context-list dd {
    overflow: hidden;
    margin: 0;
    color: var(--color-ink-soft);
    font-family: var(--font-mono);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
  }

  .actions button,
  .load-more,
  .panel-error button {
    min-height: 2rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0 0.5rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.54rem;
  }

  .actions button:disabled,
  .load-more:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .saving {
    margin-top: 0.45rem !important;
    color: var(--color-lichen) !important;
  }

  .load-more {
    width: 100%;
  }

  .context-loading {
    display: grid;
    gap: 0.65rem;
  }

  .context-loading span {
    height: 12rem;
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
