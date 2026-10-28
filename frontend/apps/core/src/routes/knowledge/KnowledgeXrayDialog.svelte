<script lang="ts">
  import type { FrontendKnowledgeXray } from "@carlina/sdk";
  import { Button, Dialog, EmptyState, Icon } from "@carlina/ui";

  import { knowledgeMetadataRows } from "../../lib/knowledge.ts";

  interface Props {
    open: boolean;
    title: string;
    xray: FrontendKnowledgeXray | null;
    loading: boolean;
    error: string;
    onclose: () => void;
    onpage: (page: number) => void;
  }

  let {
    open,
    title,
    xray,
    loading,
    error,
    onclose,
    onpage,
  }: Props = $props();

  const sourceRows = $derived(knowledgeMetadataRows(xray?.metadata));
</script>

<Dialog
  {open}
  title={`Inspect ${title}`}
  description="Compare the extracted source with the semantic chunks used during retrieval."
  {onclose}
  size="lg"
>
  {#if loading && !xray}
    <div class="xray-state" role="status">
      <Icon name="activity" size={20} />
      <span>Reading the extracted source…</span>
    </div>
  {:else if error && !xray}
    <EmptyState
      icon="file"
      title="This item could not be inspected"
      description={error}
    />
  {:else if xray}
    <div class="xray-overview">
      <div>
        <span>Source</span>
        <strong>{xray.source_type.replaceAll("_", " ")}</strong>
      </div>
      <div>
        <span>Chunks</span>
        <strong>{xray.chunk_total}</strong>
      </div>
      <div>
        <span>Source reference</span>
        <strong>{xray.source_id || "Internal item"}</strong>
      </div>
    </div>

    {#if sourceRows.length}
      <dl class="metadata-strip">
        {#each sourceRows as row (`${row.label}:${row.value}`)}
          <div>
            <dt>{row.label}</dt>
            <dd>{row.value}</dd>
          </div>
        {/each}
      </dl>
    {/if}

    <div class="xray-grid">
      <section class="xray-pane xray-source-pane" aria-labelledby="xray-source-title">
        <header>
          <p>01 / Extraction</p>
          <h3 id="xray-source-title">Readable source</h3>
        </header>
        <div class="xray-pane-body xray-source-body">
          {#if xray.raw_content}
            <pre>{xray.raw_content}</pre>
          {:else}
            <p class="empty-copy">No extracted text was retained for this item.</p>
          {/if}
        </div>
      </section>

      <section class="xray-pane xray-chunks-pane" aria-labelledby="xray-chunks-title">
        <header>
          <p>02 / Retrieval</p>
          <h3 id="xray-chunks-title">Semantic chunks</h3>
        </header>
        <div class="xray-pane-body xray-chunks-body">
          {#if loading}
            <div class="chunk-state" role="status">Loading chunk page…</div>
          {:else if error}
            <p class="inline-error" role="alert">{error}</p>
          {:else if xray.chunks.length}
            <div class="chunk-list">
              {#each xray.chunks as chunk, index (chunk.id)}
                {@const metadataRows = knowledgeMetadataRows(chunk.meta)}
                <details class="xray-chunk">
                  <summary>
                    <span class="chunk-heading">
                      <span>
                        Chunk
                        {((xray.chunk_page - 1) * 30 + index + 1)
                          .toString()
                          .padStart(2, "0")}
                      </span>
                      <small>#{chunk.id}</small>
                    </span>
                    {#if metadataRows.length}
                      <span class="chunk-summary">
                        {#each metadataRows.slice(0, 2) as row (`${chunk.id}:summary:${row.label}`)}
                          <span>{row.label}: {row.value}</span>
                        {/each}
                      </span>
                    {/if}
                  </summary>
                  <div class="chunk-body">
                    {#if metadataRows.length}
                      <dl>
                        {#each metadataRows as row (`${chunk.id}:${row.label}`)}
                          <div>
                            <dt>{row.label}</dt>
                            <dd>{row.value}</dd>
                          </div>
                        {/each}
                      </dl>
                    {/if}
                    <p>{chunk.text}</p>
                  </div>
                </details>
              {/each}
            </div>
          {:else}
            <p class="empty-copy">
              This source has no retrieval chunks yet.
            </p>
          {/if}
        </div>

        {#if xray.chunk_pages > 1}
          <nav class="pagination" aria-label="Chunk pages">
            <Button
              variant="outline"
              size="sm"
              disabled={!xray.has_prev || loading}
              onclick={() => onpage(xray.chunk_page - 1)}
            >
              <Icon name="chevron-left" size={14} />
              Previous
            </Button>
            <span>
              Page {xray.chunk_page} of {xray.chunk_pages}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={!xray.has_next || loading}
              onclick={() => onpage(xray.chunk_page + 1)}
            >
              Next
              <Icon name="chevron-right" size={14} />
            </Button>
          </nav>
        {/if}
      </section>
    </div>
  {/if}

  {#snippet footer()}
    <Button variant="quiet" onclick={onclose}>Close inspection</Button>
  {/snippet}
</Dialog>

<style>
  .xray-state {
    min-height: 16rem;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 0.75rem;
    color: var(--color-ink-muted);
    font-size: 0.78rem;
  }

  .xray-overview {
    display: grid;
    grid-template-columns: 0.75fr 0.55fr 1.7fr;
    border: 1px solid var(--color-rule-strong);
    border-right: 0;
    background: var(--color-paper);
  }

  .xray-overview div {
    min-width: 0;
    border-right: 1px solid var(--color-rule-strong);
    padding: 0.72rem 0.8rem;
  }

  .xray-overview span,
  .metadata-strip dt,
  .chunk-list dt {
    display: block;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 750;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .xray-overview strong {
    display: block;
    overflow: hidden;
    margin-top: 0.25rem;
    font-family: var(--font-editorial);
    font-size: 0.9rem;
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .metadata-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 0.55rem 1.3rem;
    margin: 0;
    border-inline: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 0.68rem 0.8rem;
  }

  .metadata-strip div {
    display: flex;
    align-items: baseline;
    gap: 0.35rem;
  }

  .metadata-strip dd,
  .chunk-list dd {
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.7rem;
  }

  .xray-grid {
    display: grid;
    grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr);
    align-items: stretch;
    gap: 1.15rem;
    margin-top: 1.15rem;
  }

  .xray-pane {
    min-width: 0;
    min-height: 0;
    height: min(34rem, 56vh);
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .xray-pane > header {
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 0.7rem 0.85rem;
  }

  .xray-pane > header p {
    margin: 0 0 0.2rem;
    color: var(--color-clay);
    font-size: 0.57rem;
    font-weight: 800;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .xray-pane > header h3 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.05rem;
    font-weight: 500;
  }

  .xray-pane-body {
    min-height: 0;
    overflow: auto;
  }

  .xray-source-body pre {
    min-height: 100%;
    box-sizing: border-box;
    margin: 0;
    border: 0;
    padding: 0.9rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-family: var(--font-sans);
    font-size: 0.72rem;
    line-height: 1.65;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .chunk-list {
    display: grid;
    gap: 0.45rem;
    padding: 0.55rem;
  }

  .xray-chunk {
    border: 1px solid var(--color-rule);
    background: color-mix(in srgb, var(--color-lichen-muted) 52%, var(--color-surface));
  }

  .xray-chunk summary {
    cursor: pointer;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.25rem;
    list-style: none;
    padding: 0.65rem 0.72rem;
  }

  .xray-chunk summary::-webkit-details-marker {
    display: none;
  }

  .xray-chunk summary::after {
    align-self: center;
    color: var(--color-ink-muted);
    content: "+";
    font-size: 1rem;
    grid-column: 2;
    grid-row: 1 / span 2;
  }

  .xray-chunk[open] summary::after {
    content: "−";
  }

  .xray-chunk summary:focus-visible {
    outline: 2px solid var(--color-focus, var(--color-clay));
    outline-offset: -2px;
  }

  .chunk-heading {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    color: var(--color-lichen);
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .chunk-heading small {
    color: var(--color-ink-muted);
    font-size: inherit;
  }

  .chunk-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 0.2rem 0.55rem;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
    line-height: 1.35;
  }

  .chunk-body {
    border-top: 1px solid var(--color-rule);
    padding: 0.68rem 0.72rem 0.75rem;
    background: var(--color-surface);
  }

  .chunk-list dl {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.8rem;
    margin: 0;
  }

  .chunk-list dl div {
    display: flex;
    gap: 0.28rem;
  }

  .chunk-list p {
    margin: 0.6rem 0 0;
    color: var(--color-ink-soft);
    font-size: 0.74rem;
    line-height: 1.58;
    white-space: pre-wrap;
  }

  .empty-copy,
  .chunk-state,
  .inline-error {
    margin: 0;
    min-height: 100%;
    box-sizing: border-box;
    padding: 1rem;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.5;
  }

  .inline-error {
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.7rem;
    background: var(--color-paper);
  }

  .pagination span {
    color: var(--color-ink-muted);
    font-size: 0.66rem;
  }

  @media (max-width: 44rem) {
    .xray-overview {
      grid-template-columns: 1fr 1fr;
    }

    .xray-overview div:last-child {
      grid-column: 1 / -1;
      border-top: 1px solid var(--color-rule-strong);
    }

    .xray-grid {
      grid-template-columns: 1fr;
    }

    .xray-pane {
      height: auto;
      min-height: 0;
      max-height: none;
    }

    .xray-source-body,
    .xray-chunks-body {
      max-height: min(22rem, 48vh);
    }

    .pagination {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
