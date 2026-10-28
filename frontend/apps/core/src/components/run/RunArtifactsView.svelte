<script lang="ts">
  import type {
    FrontendApiClient,
    FrontendArtifactDetail,
    FrontendArtifactSummary,
  } from "@carlina/sdk";
  import StatusMark from "@carlina/runtime-ui/status-mark";
  import { Button, Icon } from "@carlina/ui";

  import { formatRunDate } from "../../lib/run-studio.ts";
  import ArtifactViewer from "../context/ArtifactViewer.svelte";
  import StructuredData from "../context/StructuredData.svelte";

  interface Props {
    api: FrontendApiClient;
    artifacts: FrontendArtifactSummary[];
    detail: FrontendArtifactDetail | null;
    loading: boolean;
    error: string;
    onselect: (artifact: FrontendArtifactSummary) => void;
    onclose: () => void;
  }

  let {
    api,
    artifacts,
    detail,
    loading,
    error,
    onselect,
    onclose,
  }: Props = $props();

  function formatBytes(value: number | null): string {
    if (value === null) return "Size unknown";
    if (value < 1_024) return `${value} B`;
    if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
    return `${(value / 1_048_576).toFixed(1)} MB`;
  }
</script>

<section class="artifacts-view">
  <header>
    <div>
      <small>Run outputs</small>
      <strong>{detail ? detail.artifact.label : "Artifacts"}</strong>
    </div>
    {#if detail}
      <Button variant="quiet" size="sm" onclick={onclose}>
        <Icon name="arrow-left" size={14} />
        All artifacts
      </Button>
    {:else}
      <span>{artifacts.length} items</span>
    {/if}
  </header>

  {#if loading}
    <div class="loading" aria-busy="true"><span></span><span></span></div>
  {:else if error}
    <p class="error" role="alert">{error}</p>
  {:else if detail}
    <div class="artifact-detail">
      <dl>
        <div>
          <dt>Type</dt>
          <dd>{detail.artifact.mime_type}</dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd>{detail.artifact.version_number ?? "—"}</dd>
        </div>
        <div>
          <dt>Size</dt>
          <dd>{formatBytes(detail.artifact.size_bytes)}</dd>
        </div>
        <div>
          <dt>Review</dt>
          <dd>{detail.artifact.review_status.replace(/[_-]+/g, " ")}</dd>
        </div>
      </dl>
      <ArtifactViewer {api} {detail} />

      <div class="detail-columns">
        <section>
          <h3>Versions</h3>
          {#if detail.versions.length}
            <ol>
              {#each detail.versions as version (version.id)}
                <li>
                  <strong>Version {version.version_number}</strong>
                  <span>{version.change_summary || "No change note"}</span>
                  <time>{formatRunDate(version.created_at)}</time>
                </li>
              {/each}
            </ol>
          {:else}
            <p>No version history recorded.</p>
          {/if}
        </section>
        <section>
          <h3>Evidence and sources</h3>
          {#if detail.evidence.length}
            <ol>
              {#each detail.evidence as evidence (evidence.id)}
                <li>
                  <strong>{evidence.claim || "Recorded evidence"}</strong>
                  {#if evidence.quote}<span>{evidence.quote}</span>{/if}
                  {#if evidence.source}<small>{evidence.source.title}</small>{/if}
                </li>
              {/each}
            </ol>
          {:else if detail.sources.length}
            <ol>
              {#each detail.sources as source (source.id)}
                <li>
                  <strong>{source.title}</strong>
                  <span>{source.summary}</span>
                </li>
              {/each}
            </ol>
          {:else}
            <p>No evidence links recorded.</p>
          {/if}
        </section>
      </div>

      {#if detail.artifact.metadata}
        <details class="metadata">
          <summary>Artifact metadata</summary>
          <StructuredData value={detail.artifact.metadata} maxRows={50} />
        </details>
      {/if}
    </div>
  {:else if artifacts.length}
    <div class="artifact-grid">
      {#each artifacts as artifact (artifact.id)}
        <button type="button" onclick={() => onselect(artifact)}>
          <span class="artifact-type">{artifact.preview_kind.slice(0, 4).toUpperCase()}</span>
          <span class="artifact-copy">
            <strong>{artifact.label}</strong>
            <span>{artifact.mime_type}</span>
          </span>
          <StatusMark status={artifact.review_status} compact />
          <small>
            {formatBytes(artifact.size_bytes)} · {formatRunDate(artifact.updated_at)}
          </small>
        </button>
      {/each}
    </div>
  {:else}
    <div class="empty">
      <Icon name="file" size={24} />
      <strong>No artifacts yet</strong>
      <p>Files and durable outputs created by this run will appear here.</p>
    </div>
  {/if}
</section>

<style>
  .artifacts-view {
    min-width: 0;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .artifacts-view > header {
    min-height: 4rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem 0.9rem;
  }

  header > div {
    display: grid;
    gap: 0.18rem;
  }

  header small {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 780;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  header strong {
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
  }

  header > span {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .artifact-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
    gap: 1px;
    background: var(--color-rule);
  }

  .artifact-grid button {
    min-width: 0;
    min-height: 10rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-content: start;
    gap: 0.75rem;
    border: 0;
    padding: 0.9rem;
    background: var(--color-surface);
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .artifact-grid button:hover {
    background: var(--color-surface-muted);
  }

  .artifact-type {
    width: 2.6rem;
    height: 2.6rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.68rem;
  }

  .artifact-copy {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: 0.22rem;
  }

  .artifact-copy strong,
  .artifact-copy span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .artifact-copy strong {
    font-family: var(--font-editorial);
    font-size: 0.86rem;
    font-weight: 560;
  }

  .artifact-copy span,
  .artifact-grid button > small {
    color: var(--color-ink-muted);
    font-size: 0.57rem;
  }

  .artifact-grid button > :global(.status-mark) {
    grid-column: 1 / -1;
    justify-self: start;
  }

  .artifact-grid button > small {
    grid-column: 1 / -1;
    align-self: end;
  }

  .artifact-detail {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .artifact-detail > dl {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1px;
    margin: 0;
    background: var(--color-rule);
  }

  .artifact-detail > dl div {
    display: grid;
    gap: 0.18rem;
    padding: 0.65rem 0.8rem;
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
    font-size: 0.64rem;
  }

  .detail-columns {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1px;
    background: var(--color-rule);
  }

  .detail-columns > section {
    min-width: 0;
    padding: 0.9rem;
    background: var(--color-surface);
  }

  h3 {
    margin: 0 0 0.7rem;
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 560;
  }

  .detail-columns ol {
    display: grid;
    gap: 1px;
    margin: 0;
    padding: 0;
    background: var(--color-rule);
    list-style: none;
  }

  .detail-columns li {
    display: grid;
    gap: 0.2rem;
    padding: 0.6rem;
    background: var(--color-surface-muted);
  }

  .detail-columns strong {
    font-size: 0.65rem;
  }

  .detail-columns span,
  .detail-columns time,
  .detail-columns small,
  .detail-columns p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    line-height: 1.45;
  }

  .metadata {
    padding: 0.85rem 0.9rem;
    background: var(--color-surface);
  }

  .metadata summary {
    margin-bottom: 0.65rem;
    cursor: pointer;
    font-size: 0.66rem;
    font-weight: 700;
  }

  .loading {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .loading span {
    min-height: 12rem;
    background: var(--color-surface-muted);
  }

  .error,
  .empty {
    min-height: 18rem;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.5rem;
    margin: 0;
    padding: 1rem;
  }

  .error {
    color: var(--color-danger);
    font-size: 0.68rem;
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

  @media (max-width: 48rem) {
    .artifact-detail > dl,
    .detail-columns {
      grid-template-columns: 1fr 1fr;
    }
  }

  @media (max-width: 32rem) {
    .artifact-detail > dl,
    .detail-columns {
      grid-template-columns: 1fr;
    }
  }
</style>
