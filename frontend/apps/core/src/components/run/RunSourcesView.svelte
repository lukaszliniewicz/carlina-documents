<script lang="ts">
  import type {
    FrontendRunEvidence,
    FrontendRunSource,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import { formatRunDate } from "../../lib/run-studio.ts";
  import StructuredData from "../context/StructuredData.svelte";

  interface Props {
    sources: FrontendRunSource[];
    evidence: FrontendRunEvidence[];
  }

  let { sources, evidence }: Props = $props();
  let view = $state<"sources" | "evidence">("sources");
</script>

<section class="sources-view">
  <header>
    <div>
      <small>Provenance</small>
      <strong>Sources and evidence</strong>
    </div>
    <nav aria-label="Provenance view">
      <button type="button" class:active={view === "sources"} onclick={() => (view = "sources")}>
        Sources <span>{sources.length}</span>
      </button>
      <button type="button" class:active={view === "evidence"} onclick={() => (view = "evidence")}>
        Evidence <span>{evidence.length}</span>
      </button>
    </nav>
  </header>

  {#if view === "sources" && sources.length}
    <div class="cards">
      {#each sources as source (source.id)}
        <article>
          <header>
            <span><Icon name="book" size={17} /></span>
            <div>
              <strong>{source.title}</strong>
              <small>{source.provider || source.type}</small>
            </div>
          </header>
          <p>{source.summary || "No source summary recorded."}</p>
          <footer>
            <time>{formatRunDate(source.created_at)}</time>
            {#if source.url}
              <a href={source.url} target="_blank" rel="noreferrer">
                Open source <Icon name="arrow-up-right" size={13} />
              </a>
            {/if}
          </footer>
        </article>
      {/each}
    </div>
  {:else if view === "evidence" && evidence.length}
    <div class="cards evidence">
      {#each evidence as item (item.id)}
        <article>
          <header>
            <span><Icon name="check" size={17} /></span>
            <div>
              <strong>{item.claim || "Recorded evidence"}</strong>
              <small>{item.source?.title || "Source unavailable"}</small>
            </div>
          </header>
          {#if item.quote}<blockquote>{item.quote}</blockquote>{/if}
          {#if item.citation && Object.keys((item.citation as object) ?? {}).length}
            <StructuredData value={item.citation} compact maxRows={16} />
          {/if}
          <footer>
            <time>{formatRunDate(item.created_at)}</time>
            {#if item.source?.url}
              <a href={item.source.url} target="_blank" rel="noreferrer">
                Open source <Icon name="arrow-up-right" size={13} />
              </a>
            {/if}
          </footer>
        </article>
      {/each}
    </div>
  {:else}
    <div class="empty">
      <Icon name="book" size={24} />
      <strong>No {view} recorded</strong>
      <p>Provenance captured during execution will appear here.</p>
    </div>
  {/if}
</section>

<style>
  .sources-view {
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .sources-view > header {
    min-height: 4rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem 0.9rem;
  }

  header > div:first-child {
    display: grid;
    gap: 0.18rem;
  }

  header small {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 780;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  header strong {
    font-family: var(--font-editorial);
    font-size: 0.94rem;
    font-weight: 560;
  }

  header nav {
    display: flex;
    border: 1px solid var(--color-rule);
  }

  header nav button {
    min-height: 2.1rem;
    display: flex;
    align-items: center;
    gap: 0.35rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.4rem 0.58rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.6rem;
  }

  header nav button:last-child {
    border-right: 0;
  }

  header nav button.active {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  header nav span {
    color: var(--color-clay);
    font-size: 0.55rem;
  }

  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr));
    gap: 1px;
    background: var(--color-rule);
  }

  .cards article {
    min-width: 0;
    min-height: 12rem;
    display: grid;
    align-content: start;
    gap: 0.8rem;
    padding: 0.9rem;
    background: var(--color-surface);
  }

  .cards article > header {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 0.65rem;
  }

  .cards article > header > span {
    width: 2.35rem;
    height: 2.35rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
  }

  .cards article > header > div {
    min-width: 0;
    display: grid;
    gap: 0.18rem;
  }

  .cards article > header strong,
  .cards article > header small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cards article > p,
  blockquote {
    margin: 0;
    color: var(--color-ink-soft);
    font-family: var(--font-editorial);
    font-size: 0.78rem;
    line-height: 1.55;
  }

  blockquote {
    border-left: 2px solid var(--color-clay);
    padding-left: 0.7rem;
  }

  .cards footer {
    align-self: end;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-top: auto;
    padding-top: 0.7rem;
    border-top: 1px solid var(--color-rule);
  }

  .cards time {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .cards a {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    color: var(--color-clay);
    font-size: 0.59rem;
    font-weight: 700;
    text-decoration: none;
  }

  .empty {
    min-height: 18rem;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.5rem;
    padding: 1rem;
  }

  .empty strong {
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
  }

  .empty p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.67rem;
  }

  @media (max-width: 40rem) {
    .sources-view > header {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
