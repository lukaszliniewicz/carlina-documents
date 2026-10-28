<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendBootstrap,
    FrontendLogChunk,
    FrontendLogSource,
  } from "@carlina/sdk";
  import {
    Button,
    Dialog,
    EmptyState,
    Icon,
    IconButton,
    PageHeader,
    SearchField,
  } from "@carlina/ui";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (href: string, clientRoute?: boolean) => void;
  }

  let { api, route, navigate }: Props = $props();

  let sources = $state<FrontendLogSource[]>([]);
  let selectedId = $state("");
  let sourceQuery = $state("");
  let entryQuery = $state("");
  let logText = $state("");
  let nextCursor = $state(0);
  let loadingSources = $state(true);
  let loadingLog = $state(false);
  let loadingOlder = $state(false);
  let error = $state("");
  let warning = $state("");
  let clearOpen = $state(false);
  let viewerExpanded = $state(false);
  let clearing = $state(false);
  let announcement = $state("");
  let requestController: AbortController | null = null;
  let requestSequence = 0;

  const filteredSources = $derived.by(() => {
    const needle = sourceQuery.trim().toLocaleLowerCase();
    if (!needle) return sources;
    return sources.filter((source) =>
      `${source.name} ${source.description} ${source.category} ${source.id}`
        .toLocaleLowerCase()
        .includes(needle),
    );
  });
  const selectedSource = $derived(
    sources.find((source) => source.id === selectedId),
  );
  const visibleLines = $derived.by(() => {
    if (!logText) return [];
    const lines = logText.split(/\r?\n/u);
    const needle = entryQuery.trim().toLocaleLowerCase();
    if (!needle) return lines;
    return lines.filter((line) => line.toLocaleLowerCase().includes(needle));
  });
  const visibleLogText = $derived(visibleLines.join("\n"));

  onMount(() => {
    void loadSources();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && viewerExpanded && !clearOpen) {
        viewerExpanded = false;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      requestController?.abort();
      window.removeEventListener("keydown", onKeyDown);
    };
  });

  $effect(() => {
    if (!viewerExpanded || typeof document === "undefined") return;
    const previousBodyOverflow = document.body.style.overflow;
    const previousRootOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.documentElement.style.overflow = previousRootOverflow;
    };
  });

  $effect(() => {
    if (!sources.length) return;
    const requested = new URL(route, "https://iarf.local").searchParams.get(
      "source",
    );
    const desired =
      sources.find((source) => source.id === requested)?.id ??
      sources.find((source) => source.exists && source.readable)?.id ??
      sources[0]?.id ??
      "";
    if (desired && desired !== selectedId) {
      selectedId = desired;
      void loadLog(desired);
    }
  });

  function sourceApiUrl(sourceId: string): string {
    return `/api/logs/${encodeURIComponent(sourceId)}`;
  }

  async function loadSources() {
    loadingSources = true;
    error = "";
    try {
      const payload = await api.request<{ sources: FrontendLogSource[] }>(
        "/api/log-sources",
      );
      sources = payload.sources;
      const requested = new URL(route, "https://iarf.local").searchParams.get(
        "source",
      );
      const preferred =
        sources.find((source) => source.id === requested)?.id ??
        sources.find((source) => source.exists && source.readable)?.id ??
        sources[0]?.id ??
        "";
      if (preferred) {
        selectedId = preferred;
        await loadLog(preferred);
      }
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "Log sources could not be loaded.";
    } finally {
      loadingSources = false;
    }
  }

  async function loadLog(sourceId: string, older = false) {
    requestController?.abort();
    requestController = new AbortController();
    const requestId = ++requestSequence;
    if (older) {
      loadingOlder = true;
    } else {
      loadingLog = true;
      warning = "";
      entryQuery = "";
    }
    error = "";
    try {
      const cursorQuery =
        older && nextCursor > 0 ? `?end_byte=${nextCursor}` : "";
      const chunk = await api.request<FrontendLogChunk>(
        `${sourceApiUrl(sourceId)}${cursorQuery}`,
        { signal: requestController.signal },
      );
      if (sourceId !== selectedId || requestId !== requestSequence) return;
      logText = older
        ? `${chunk.text}${chunk.text && logText ? "\n" : ""}${logText}`
        : chunk.text;
      nextCursor = chunk.next_cursor;
      warning = chunk.warning ?? "";
      sources = sources.map((source) =>
        source.id === chunk.source.id ? chunk.source : source,
      );
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      error =
        reason instanceof Error ? reason.message : "The selected log could not be read.";
    } finally {
      if (requestId === requestSequence) {
        loadingLog = false;
        loadingOlder = false;
      }
    }
  }

  function selectSource(sourceId: string) {
    if (sourceId === selectedId) return;
    selectedId = sourceId;
    const next = new URL("/logs", "https://iarf.local");
    next.searchParams.set("source", sourceId);
    navigate(`${next.pathname}${next.search}`, true);
    void loadLog(sourceId);
  }

  async function clearSelectedSource() {
    if (!selectedSource) return;
    clearing = true;
    error = "";
    try {
      await api.request<{ success: boolean; message: string }>(
        sourceApiUrl(selectedSource.id),
        { method: "DELETE" },
      );
      announcement = `${selectedSource.name} was cleared.`;
      clearOpen = false;
      await loadLog(selectedSource.id);
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "The log could not be cleared.";
    } finally {
      clearing = false;
    }
  }

  function formatBytes(bytes: number): string {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(
      Math.floor(Math.log(bytes) / Math.log(1024)),
      units.length - 1,
    );
    return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function formatDate(value: string | null): string {
    if (!value) return "Not created";
    const date = new Date(value);
    return Number.isNaN(date.valueOf())
      ? value
      : new Intl.DateTimeFormat(undefined, {
          dateStyle: "medium",
          timeStyle: "short",
        }).format(date);
  }

</script>

<div class="logs-page">
  <PageHeader
    eyebrow="Administration / Observability"
    title="Logs"
    description="Read the original service output as plain text and move quickly between registered sources."
  />

  <div class="log-workbench" class:log-workbench--expanded={viewerExpanded}>
    <aside class="source-panel" aria-label="Log sources">
      <div class="source-panel__heading">
        <div>
          <p>Sources</p>
          <span>{sources.length}</span>
        </div>
        <IconButton
          icon="refresh"
          label="Refresh sources"
          disabled={loadingSources}
          onclick={() => void loadSources()}
        />
      </div>
      <div class="source-search">
        <SearchField
          id="log-source-search"
          value={sourceQuery}
          label="Filter log sources"
          placeholder="Filter sources…"
          oninput={(value) => (sourceQuery = value)}
        />
      </div>
      <div class="source-list">
        {#if loadingSources}
          <p class="panel-message">Reading source registry…</p>
        {:else if filteredSources.length}
          {#each filteredSources as source (source.id)}
            <button
              class:active={source.id === selectedId}
              onclick={() => selectSource(source.id)}
            >
              <span
                class="source-status"
                class:available={source.exists && source.readable}
                aria-hidden="true"
              ></span>
              <span class="source-copy">
                <strong>{source.name}</strong>
                <small>{source.category} · {formatBytes(source.size)}</small>
              </span>
              <Icon name="chevron-right" size={15} />
            </button>
          {/each}
        {:else}
          <p class="panel-message">No source matches this filter.</p>
        {/if}
      </div>
    </aside>

    <section class="log-viewer" aria-busy={loadingLog}>
      {#if selectedSource}
        <header class="viewer-header">
          <div class="viewer-title">
            <p>{selectedSource.category}</p>
            <h2>{selectedSource.name}</h2>
            <span>{selectedSource.description || selectedSource.id}</span>
          </div>
          <div class="viewer-actions">
            <IconButton
              icon="crop"
              label={viewerExpanded ? "Exit expanded log viewer" : "Expand log viewer"}
              aria-pressed={viewerExpanded}
              onclick={() => (viewerExpanded = !viewerExpanded)}
            />
            <IconButton
              icon="refresh"
              label="Refresh selected log"
              disabled={loadingLog}
              onclick={() => void loadLog(selectedSource.id)}
            />
            {#if selectedSource.downloadable && selectedSource.exists}
              <a
                class="icon-link"
                href={`${sourceApiUrl(selectedSource.id)}/download`}
                aria-label={`Download ${selectedSource.name}`}
                title="Download log"
              >
                <Icon name="download" size={18} />
              </a>
            {/if}
            {#if selectedSource.clearable}
              <IconButton
                icon="trash"
                label="Clear selected log"
                onclick={() => (clearOpen = true)}
              />
            {/if}
          </div>
        </header>

        <div class="source-facts">
          <span><strong>{formatBytes(selectedSource.size)}</strong> size</span>
          <span><strong>{formatDate(selectedSource.modified_at)}</strong> modified</span>
          <span>
            <strong>{selectedSource.exists && selectedSource.readable ? "Available" : "Waiting"}</strong>
            state
          </span>
        </div>

        <div class="entry-tools">
          <SearchField
            id="log-entry-search"
            value={entryQuery}
            label="Filter visible log lines"
            placeholder="Filter the loaded text…"
            oninput={(value) => (entryQuery = value)}
          />
          <p>{visibleLines.length} lines visible</p>
        </div>

        {#if error}
          <p class="feedback feedback--error" role="alert">{error}</p>
        {:else if warning}
          <p class="feedback" role="status">{warning}</p>
        {/if}

        {#if loadingLog}
          <div class="timeline-loading" aria-label="Loading log entries">
            <span></span><span></span><span></span><span></span>
          </div>
        {:else if visibleLogText}
          <div class="plain-log-wrap">
            {#if nextCursor > 0 && !entryQuery}
              <div class="older">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={loadingOlder}
                  onclick={() => void loadLog(selectedSource.id, true)}
                >
                  <Icon name="arrow-left" size={14} />
                  {loadingOlder ? "Loading…" : "Load earlier entries"}
                </Button>
              </div>
            {/if}
            <pre
              class="plain-log"
              aria-label={`${selectedSource.name} plain-text log`}
            >{visibleLogText}</pre>
          </div>
        {:else}
          <EmptyState
            icon={entryQuery ? "search" : "logs"}
            title={entryQuery ? "No matching entries" : "Nothing has been written yet"}
            description={entryQuery
              ? "Clear the text filter to see all loaded lines."
              : "This source is registered and ready. New activity will appear here."}
          >
            {#snippet actions()}
              {#if entryQuery}
                <button class="text-action" onclick={() => (entryQuery = "")}>Clear filter</button>
              {:else}
                <button class="text-action" onclick={() => void loadLog(selectedSource.id)}>Refresh source</button>
              {/if}
            {/snippet}
          </EmptyState>
        {/if}
      {:else if !loadingSources}
        <EmptyState
          icon="logs"
          title="No log source selected"
          description="Choose a registered source to inspect its activity."
        />
      {/if}
    </section>
  </div>

  <p class="sr-announcement" aria-live="polite">{announcement}</p>
</div>

<Dialog
  open={clearOpen}
  title="Clear this log?"
  description="This removes the current file contents. The source remains registered and can receive new entries."
  onclose={() => (clearOpen = false)}
  size="sm"
>
  <p class="confirm-copy">
    You are about to clear <strong>{selectedSource?.name}</strong>. Download it
    first if you need to retain a copy.
  </p>
  {#snippet footer()}
    <Button variant="quiet" disabled={clearing} onclick={() => (clearOpen = false)}>
      Cancel
    </Button>
    <Button variant="danger" disabled={clearing} onclick={() => void clearSelectedSource()}>
      {clearing ? "Clearing…" : "Clear log"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .logs-page {
    width: min(100%, 100rem);
    min-height: calc(100dvh - var(--shell-header-height));
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3.5rem);
  }

  .text-action,
  .icon-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
    color: var(--color-ink);
    text-decoration: none;
  }

  .icon-link {
    width: 2.5rem;
    height: 2.5rem;
  }

  .icon-link:hover,
  .text-action:hover {
    background: var(--color-surface-muted);
  }

  .log-workbench {
    min-height: clamp(32rem, calc(100dvh - 17rem), 60rem);
    display: grid;
    flex: 1;
    grid-template-columns: minmax(14rem, 19rem) minmax(0, 1fr);
    overflow: hidden;
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.5rem;
    background: var(--color-surface);
  }

  .log-workbench--expanded {
    min-height: 0;
    position: fixed;
    inset: 0;
    z-index: 20;
    margin: 0;
    border: 0;
  }

  .source-panel {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .source-panel__heading {
    min-height: 4rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.6rem 0.7rem 0.6rem 1rem;
  }

  .source-panel__heading > div {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
  }

  .source-panel__heading p {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.1rem;
    font-weight: 550;
  }

  .source-panel__heading span {
    color: var(--color-clay);
    font-size: 0.63rem;
  }

  .source-search {
    border-bottom: 1px solid var(--color-rule);
    padding: 0.7rem;
  }

  .source-list {
    min-height: 0;
    flex: 1;
    overflow-y: auto;
  }

  .source-list button {
    width: 100%;
    min-height: 4.15rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0.8rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .source-list button:hover,
  .source-list button.active {
    background: var(--color-clay-muted);
  }

  .source-list button.active {
    box-shadow: inset 3px 0 0 var(--color-clay);
  }

  .source-status {
    width: 0.48rem;
    height: 0.48rem;
    border-radius: 50%;
    background: var(--color-rule-strong);
  }

  .source-status.available {
    background: var(--color-lichen);
  }

  .source-copy {
    min-width: 0;
    display: grid;
    gap: 0.2rem;
  }

  .source-copy strong,
  .source-copy small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .source-copy strong {
    font-size: 0.75rem;
    font-weight: 650;
  }

  .source-copy small {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    text-transform: capitalize;
  }

  .panel-message {
    margin: 0;
    padding: 1.25rem 1rem;
    color: var(--color-ink-muted);
    font-size: 0.72rem;
    line-height: 1.5;
  }

  .log-viewer {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--color-surface);
  }

  .viewer-header {
    min-height: 5.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem 1.1rem;
  }

  .viewer-title {
    min-width: 0;
  }

  .viewer-title p {
    margin: 0 0 0.2rem;
    color: var(--color-clay);
    font-size: 0.57rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .viewer-title h2 {
    overflow: hidden;
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.45rem;
    font-weight: 520;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .viewer-title > span {
    display: block;
    overflow: hidden;
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .viewer-actions {
    display: flex;
    flex: 0 0 auto;
    gap: 0.25rem;
  }

  .source-facts {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border-bottom: 1px solid var(--color-rule);
    background: var(--color-paper);
  }

  .source-facts span {
    min-height: 3.35rem;
    display: grid;
    align-content: center;
    gap: 0.1rem;
    border-right: 1px solid var(--color-rule);
    padding: 0.55rem 0.9rem;
    color: var(--color-ink-muted);
    font-size: 0.57rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .source-facts span:last-child {
    border-right: 0;
  }

  .source-facts strong {
    overflow: hidden;
    color: var(--color-ink-soft);
    font-size: 0.67rem;
    letter-spacing: 0;
    text-overflow: ellipsis;
    text-transform: none;
    white-space: nowrap;
  }

  .entry-tools {
    display: grid;
    grid-template-columns: minmax(12rem, 25rem) auto;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem 0.9rem;
  }

  .entry-tools p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .feedback {
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.7rem 1rem;
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
    font-size: 0.7rem;
  }

  .feedback--error {
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .plain-log-wrap {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
  }

  .older {
    display: flex;
    justify-content: center;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem;
  }

  .plain-log {
    min-height: 0;
    flex: 1;
    overflow: auto;
    margin: 0;
    padding: 0.85rem 1rem 1.2rem;
    background: var(--color-paper);
    color: var(--color-ink-soft);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.65rem;
    line-height: 1.42;
    overflow-wrap: anywhere;
    tab-size: 2;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .timeline-loading {
    min-height: 0;
    display: grid;
    flex: 1;
    gap: 1px;
    background: var(--color-rule);
  }

  .timeline-loading span {
    min-height: 4rem;
    background: linear-gradient(
      90deg,
      var(--color-paper),
      var(--color-surface),
      var(--color-paper)
    );
    background-size: 200% 100%;
    animation: loading 1.5s ease-in-out infinite;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  .text-action {
    min-height: 2.4rem;
    padding: 0.5rem 0.7rem;
    cursor: pointer;
    font-size: 0.7rem;
    font-weight: 680;
  }

  .confirm-copy {
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.82rem;
    line-height: 1.6;
  }

  .sr-announcement {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
  }

  @media (max-width: 74rem) {
    .log-workbench {
      grid-template-columns: minmax(12rem, 15.5rem) minmax(0, 1fr);
    }
  }

  @media (max-width: 52rem) {
    .log-workbench {
      grid-template-columns: 1fr;
      grid-template-rows: auto minmax(0, 1fr);
    }

    .source-panel {
      max-height: 15rem;
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }

    .source-list {
      max-height: 10rem;
    }

    .log-workbench--expanded {
      min-height: 0;
    }
  }

  @media (max-width: 36rem) {
    .logs-page {
      padding-inline: 0.75rem;
    }

    .source-facts {
      grid-template-columns: 1fr;
    }

    .source-facts span {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .entry-tools {
      grid-template-columns: 1fr;
    }

    .plain-log {
      font-size: 0.61rem;
    }
  }
</style>
