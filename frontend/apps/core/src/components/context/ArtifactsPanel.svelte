<script lang="ts">
  import { onDestroy, type Component } from "svelte";
  import type {
    FrontendApiClient,
    FrontendArtifactDetail,
    FrontendArtifactSummary,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import {
    formatWorkspaceBytes,
    humanizeWorkspaceLabel,
  } from "../../lib/workspace-state.ts";
  import StructuredData from "./StructuredData.svelte";

  interface ViewerProps {
    api: FrontendApiClient;
    detail: FrontendArtifactDetail;
  }

  interface Props {
    api: FrontendApiClient;
    items: FrontendArtifactSummary[];
    total: number;
    nextCursor: string | null;
    searchTruncated: boolean;
    loading: boolean;
    loadingMore: boolean;
    error: string;
    status: string;
    query: string;
    detail: FrontendArtifactDetail | null;
    detailLoading: boolean;
    detailError: string;
    reviewing: boolean;
    onchangefilter: (status: string) => void;
    onquery: (query: string) => void;
    onloadmore: () => void;
    onrefresh: () => void;
    onselect: (artifact: FrontendArtifactSummary) => void;
    onclose: () => void;
    onreview: (status: string, note: string) => void;
  }

  let {
    api,
    items,
    total,
    nextCursor,
    searchTruncated,
    loading,
    loadingMore,
    error,
    status,
    query,
    detail,
    detailLoading,
    detailError,
    reviewing,
    onchangefilter,
    onquery,
    onloadmore,
    onrefresh,
    onselect,
    onclose,
    onreview,
  }: Props = $props();

  let localQuery = $state("");
  let searchTimer: number | null = null;
  let reviewNote = $state("");
  let Viewer = $state<Component<ViewerProps> | null>(null);

  const filters = [
    { id: "all", label: "All" },
    { id: "pending_review", label: "Review" },
    { id: "draft", label: "Drafts" },
    { id: "approved", label: "Approved" },
  ];

  $effect(() => {
    if (query !== localQuery && searchTimer === null) localQuery = query;
  });

  $effect(() => {
    const artifactId = detail?.artifact.id;
    if (!artifactId) {
      Viewer = null;
      reviewNote = "";
      return;
    }
    let current = true;
    reviewNote =
      typeof detail.artifact.review.note === "string"
        ? detail.artifact.review.note
        : "";
    void import("./ArtifactViewer.svelte").then((module) => {
      if (current) Viewer = module.default;
    });
    return () => {
      current = false;
    };
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
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "";
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year:
        date.getFullYear() === new Date().getFullYear()
          ? undefined
          : "numeric",
    }).format(date);
  }

  function previewInitials(artifact: FrontendArtifactSummary): string {
    if (artifact.preview_kind === "markdown") return "MD";
    if (artifact.preview_kind === "structured") return "DATA";
    if (artifact.preview_kind === "image") return "IMG";
    if (artifact.preview_kind === "audio") return "AUD";
    if (artifact.preview_kind === "video") return "VID";
    if (artifact.preview_kind === "pdf") return "PDF";
    return artifact.type.slice(0, 4).toUpperCase();
  }
</script>

<div
  id="context-panel-artifacts"
  class="artifacts"
  role="tabpanel"
  aria-labelledby="context-tab-artifacts"
>
  {#if detail || detailLoading || detailError}
    <header class="detail-heading">
      <button type="button" onclick={onclose}>
        <Icon name="arrow-left" size={14} /> Back to artifacts
      </button>
      {#if detail}
        <span>{humanizeWorkspaceLabel(detail.artifact.review_status)}</span>
      {/if}
    </header>

    {#if detailLoading}
      <div class="detail-loading" aria-busy="true" aria-label="Loading artifact">
        <span></span><span></span><span></span>
      </div>
    {:else if detailError}
      <div class="panel-error" role="alert">
        <Icon name="activity" size={16} />
        <p>{detailError}</p>
        <button type="button" onclick={onclose}>Return to gallery</button>
      </div>
    {:else if detail}
      <article class="artifact-detail">
        <header>
          <span aria-hidden="true">{previewInitials(detail.artifact)}</span>
          <div>
            <p>{humanizeWorkspaceLabel(detail.artifact.preview_kind)}</p>
            <h3>{detail.artifact.label}</h3>
            <small>
              {formatWorkspaceBytes(detail.artifact.size_bytes)}
              {#if detail.artifact.version_number}
                · Version {detail.artifact.version_number}
              {/if}
              {#if detail.artifact.source_addon_id}
                · {humanizeWorkspaceLabel(detail.artifact.source_addon_id)}
              {/if}
            </small>
          </div>
        </header>

        <div class="artifact-links">
          {#if detail.artifact.download_url}
            <a href={detail.artifact.download_url}>
              <Icon name="download" size={13} /> Download
            </a>
          {/if}
          {#if detail.artifact.external_url}
            <a
              href={detail.artifact.external_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Icon name="arrow-up-right" size={13} /> Open source
            </a>
          {/if}
        </div>

        {#if Viewer}
          <Viewer {api} {detail} />
        {:else}
          <div class="viewer-placeholder" aria-busy="true">
            Preparing the appropriate viewer…
          </div>
        {/if}

        {#if detail.artifact.review.can_review}
          <section class="review-panel">
            <header>
              <div>
                <p>Editorial decision</p>
                <h4>Review this artifact</h4>
              </div>
              <span>{humanizeWorkspaceLabel(detail.artifact.review.status)}</span>
            </header>
            <label>
              <span>Review note <i>optional</i></span>
              <textarea
                rows="3"
                bind:value={reviewNote}
                placeholder="Record a concise reason or requested change."
              ></textarea>
            </label>
            <div>
              <button
                type="button"
                disabled={reviewing}
                onclick={() => onreview("rejected", reviewNote)}
              >
                Reject
              </button>
              <button
                type="button"
                disabled={reviewing}
                onclick={() => onreview("changes_requested", reviewNote)}
              >
                Request changes
              </button>
              <button
                class="approve"
                type="button"
                disabled={reviewing}
                onclick={() => onreview("approved", reviewNote)}
              >
                <Icon name="check" size={13} />
                {reviewing ? "Saving…" : "Approve"}
              </button>
            </div>
          </section>
        {:else if detail.artifact.review.note}
          <section class="review-note">
            <p>Review note</p>
            <blockquote>{String(detail.artifact.review.note)}</blockquote>
          </section>
        {/if}

        {#if
          detail.artifact.metadata &&
          typeof detail.artifact.metadata === "object" &&
          Object.keys(detail.artifact.metadata as object).length}
          <section class="detail-section">
            <header>
              <p>About this artifact</p>
              <h4>Metadata</h4>
            </header>
            <StructuredData value={detail.artifact.metadata} maxRows={40} />
          </section>
        {/if}

        <section class="detail-section">
          <header>
            <p>{detail.versions.length} retained</p>
            <h4>Version history</h4>
          </header>
          {#if detail.versions.length}
            <ol class="version-list">
              {#each detail.versions as version (version.id)}
                <li class:version--current={version.current}>
                  <span>v{version.version_number}</span>
                  <div>
                    <strong>
                      {version.current ? "Current version" : version.label}
                    </strong>
                    <small>
                      {dateLabel(version.created_at)} ·
                      {formatWorkspaceBytes(version.size_bytes)}
                    </small>
                    {#if version.change_summary}
                      <p>{String(version.change_summary)}</p>
                    {/if}
                  </div>
                  <span>
                    {version.source_count} src · {version.evidence_count} ev
                  </span>
                </li>
              {/each}
            </ol>
            {#if detail.versions_truncated}
              <p class="section-note">
                Showing the 50 most recent versions.
              </p>
            {/if}
          {:else}
            <p class="section-note">No version records have been added yet.</p>
          {/if}
        </section>

        <section class="detail-section">
          <header>
            <p>{detail.sources.length} linked</p>
            <h4>Sources</h4>
          </header>
          {#if detail.sources.length}
            <ul class="source-list">
              {#each detail.sources as source (source.id)}
                <li>
                  <span><Icon name="book" size={14} /></span>
                  <div>
                    <strong>{source.title}</strong>
                    <small>
                      {source.provider ||
                        humanizeWorkspaceLabel(source.type)}
                      {source.created_at ? ` · ${dateLabel(source.created_at)}` : ""}
                    </small>
                    {#if source.summary}<p>{source.summary}</p>{/if}
                  </div>
                  {#if source.url}
                    <a
                      href={source.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`Open source ${source.title}`}
                    >
                      <Icon name="arrow-up-right" size={13} />
                    </a>
                  {/if}
                </li>
              {/each}
            </ul>
          {:else}
            <p class="section-note">
              No source records are explicitly linked to this version.
            </p>
          {/if}
        </section>

        <section class="detail-section">
          <header>
            <p>{detail.evidence.length} records</p>
            <h4>Evidence</h4>
          </header>
          {#if detail.evidence.length}
            <div class="evidence-list">
              {#each detail.evidence as evidence (evidence.id)}
                <article>
                  <header>
                    <span class:evidence--used={evidence.used_in_artifact}>
                      {evidence.used_in_artifact ? "Used" : "Linked"}
                    </span>
                    {#if evidence.relevance_score !== null}
                      <small>
                        {Math.round(evidence.relevance_score * 100)}% relevance
                      </small>
                    {/if}
                  </header>
                  {#if evidence.claim}<h5>{evidence.claim}</h5>{/if}
                  {#if evidence.quote}<blockquote>{evidence.quote}</blockquote>{/if}
                  {#if evidence.source}
                    <p>
                      Source ·
                      {#if evidence.source.url}
                        <a
                          href={evidence.source.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {evidence.source.title}
                        </a>
                      {:else}
                        {evidence.source.title}
                      {/if}
                    </p>
                  {/if}
                  {#if
                    evidence.citation &&
                    typeof evidence.citation === "object" &&
                    Object.keys(evidence.citation as object).length}
                    <StructuredData
                      value={evidence.citation}
                      maxRows={12}
                      compact
                    />
                  {/if}
                </article>
              {/each}
            </div>
          {:else}
            <p class="section-note">
              No evidence records are explicitly linked to this artifact.
            </p>
          {/if}
        </section>
      </article>
    {/if}
  {:else}
    <header class="panel-heading">
      <div>
        <p>{total} in this conversation</p>
        <h3>Artifacts</h3>
      </div>
      <span>Lazy previews</span>
    </header>

    <label class="search-field">
      <Icon name="search" size={14} />
      <span class="sr-only">Search artifacts</span>
      <input
        type="search"
        value={localQuery}
        placeholder="Search artifacts"
        oninput={(event) =>
          changeQuery((event.currentTarget as HTMLInputElement).value)}
      />
      {#if localQuery}
        <button type="button" aria-label="Clear search" onclick={() => changeQuery("")}>
          <Icon name="close" size={12} />
        </button>
      {/if}
    </label>

    <div class="artifact-filters" role="group" aria-label="Artifact status">
      {#each filters as option (option.id)}
        <button
          type="button"
          class:is-active={status === option.id}
          aria-pressed={status === option.id}
          onclick={() => onchangefilter(option.id)}
        >
          {option.label}
        </button>
      {/each}
    </div>

    {#if searchTruncated}
      <p class="search-note">
        Search is limited to the 1,000 most recent artifacts.
      </p>
    {/if}

    {#if loading && !items.length}
      <div class="artifact-loading" aria-busy="true" aria-label="Loading artifacts">
        <span></span><span></span><span></span><span></span>
      </div>
    {:else if error && !items.length}
      <div class="panel-error" role="alert">
        <Icon name="activity" size={16} />
        <p>{error}</p>
        <button type="button" onclick={onrefresh}>Try again</button>
      </div>
    {:else if !items.length}
      <div class="empty">
        <span><Icon name="file" size={19} /></span>
        <h4>{query ? "No matching artifacts" : "No artifacts yet"}</h4>
        <p>
          {query
            ? "Try another title, type, or review status."
            : "Files and durable outputs created during this conversation will appear here."}
        </p>
      </div>
    {:else}
      <div class="artifact-grid">
        {#each items as artifact (artifact.id)}
          <button type="button" onclick={() => onselect(artifact)}>
            <span class={`preview preview--${artifact.preview_kind}`}>
              <b>{previewInitials(artifact)}</b>
              <i></i><i></i><i></i>
            </span>
            <span class="artifact-copy">
              <strong>{artifact.label}</strong>
              <small>
                {humanizeWorkspaceLabel(artifact.preview_kind)}
                {#if artifact.version_number}
                  · Version {artifact.version_number}
                {/if}
                · {formatWorkspaceBytes(artifact.size_bytes)}
              </small>
            </span>
            <span class={`review-badge review-badge--${artifact.review_status}`}>
              {humanizeWorkspaceLabel(artifact.review_status)}
            </span>
          </button>
        {/each}
      </div>

      {#if nextCursor}
        <button
          class="load-more"
          type="button"
          disabled={loadingMore}
          onclick={onloadmore}
        >
          {loadingMore ? "Loading more artifacts…" : "Load more artifacts"}
        </button>
      {/if}
    {/if}
  {/if}
</div>

<style>
  .artifacts {
    display: grid;
    align-content: start;
    gap: 0.75rem;
    padding: 1.2rem 1rem 3rem;
  }

  .panel-heading,
  .detail-heading,
  .artifact-detail > header,
  .review-panel > header,
  .detail-section > header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .panel-heading p,
  .detail-section > header p,
  .review-panel > header p {
    margin: 0 0 0.15rem;
    color: var(--color-clay);
    font-size: 0.54rem;
    font-weight: 750;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .panel-heading h3,
  .artifact-detail > header h3 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.22rem;
    font-weight: 540;
  }

  .panel-heading > span {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
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

  .artifact-filters {
    display: flex;
    gap: 1px;
    background: var(--color-rule);
  }

  .artifact-filters button {
    min-height: 2rem;
    flex: 1;
    border: 0;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.56rem;
  }

  .artifact-filters button.is-active {
    background: var(--color-clay-muted);
    color: var(--color-clay);
    font-weight: 750;
  }

  .search-note {
    margin: -0.1rem 0 0;
    color: var(--color-tobacco);
    font-size: 0.53rem;
  }

  .artifact-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.6rem;
  }

  .artifact-grid > button {
    min-width: 0;
    display: grid;
    align-content: start;
    border: 1px solid var(--color-rule);
    padding: 0;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    cursor: pointer;
    text-align: left;
    content-visibility: auto;
    contain-intrinsic-size: auto 11rem;
  }

  .artifact-grid > button:hover {
    border-color: var(--color-rule-strong);
  }

  .preview {
    position: relative;
    height: 6.5rem;
    display: grid;
    align-content: center;
    gap: 0.34rem;
    padding: 0.8rem;
    overflow: hidden;
    background: var(--color-paper);
  }

  .preview b {
    position: absolute;
    top: 0.5rem;
    left: 0.5rem;
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.5rem;
  }

  .preview i {
    width: 82%;
    height: 2px;
    display: block;
    background: var(--color-rule-strong);
  }

  .preview i:nth-of-type(2) {
    width: 65%;
  }

  .preview i:nth-of-type(3) {
    width: 75%;
  }

  .preview--image,
  .preview--video {
    background:
      linear-gradient(145deg, transparent 45%, var(--color-lichen-muted) 46% 70%, transparent 71%),
      linear-gradient(35deg, transparent 42%, var(--color-clay-muted) 43% 68%, transparent 69%),
      var(--color-paper);
  }

  .preview--audio i {
    width: 100%;
    height: 1px;
    transform: rotate(-6deg);
  }

  .preview--structured {
    background:
      linear-gradient(var(--color-rule) 1px, transparent 1px),
      linear-gradient(90deg, var(--color-rule) 1px, transparent 1px),
      var(--color-paper);
    background-size: 18px 18px;
  }

  .artifact-copy {
    min-width: 0;
    display: grid;
    gap: 0.18rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.55rem;
  }

  .artifact-copy strong,
  .artifact-copy small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .artifact-copy strong {
    font-size: 0.63rem;
  }

  .artifact-copy small {
    color: var(--color-ink-muted);
    font-size: 0.5rem;
  }

  .review-badge {
    justify-self: start;
    margin: 0 0.55rem 0.55rem;
    padding: 0.22rem 0.38rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.48rem;
    font-weight: 720;
  }

  .review-badge--draft,
  .review-badge--pending_review,
  .review-badge--changes_requested {
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
  }

  .review-badge--approved {
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
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

  .detail-heading {
    align-items: center;
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 0.65rem;
  }

  .detail-heading button {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.38rem;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.58rem;
    font-weight: 700;
  }

  .detail-heading > span {
    padding: 0.25rem 0.4rem;
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
    font-size: 0.5rem;
    font-weight: 720;
  }

  .artifact-detail {
    min-width: 0;
    display: grid;
    gap: 0.85rem;
  }

  .artifact-detail > header {
    align-items: center;
  }

  .artifact-detail > header > span {
    width: 3rem;
    height: 3.2rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-clay-muted);
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
    font-weight: 750;
  }

  .artifact-detail > header > div {
    min-width: 0;
    flex: 1;
  }

  .artifact-detail > header p {
    margin: 0 0 0.12rem;
    color: var(--color-clay);
    font-size: 0.52rem;
    font-weight: 720;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .artifact-detail > header h3 {
    overflow-wrap: anywhere;
    font-size: 1.05rem;
  }

  .artifact-detail > header small {
    color: var(--color-ink-muted);
    font-size: 0.53rem;
  }

  .artifact-links {
    display: flex;
    gap: 0.45rem;
  }

  .artifact-links a {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.5rem;
    color: var(--color-ink-soft);
    font-size: 0.56rem;
    text-decoration: none;
  }

  .viewer-placeholder {
    min-height: 9rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: 0.6rem;
  }

  .review-panel {
    border: 1px solid color-mix(in srgb, var(--color-tobacco) 38%, var(--color-rule));
    padding: 0.75rem;
    background: color-mix(
      in srgb,
      var(--color-tobacco-muted) 42%,
      var(--color-surface)
    );
  }

  .review-panel > header {
    align-items: center;
  }

  .review-panel h4,
  .detail-section h4 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 0.9rem;
    font-weight: 560;
  }

  .review-panel > header > span {
    color: var(--color-tobacco);
    font-size: 0.52rem;
    font-weight: 720;
  }

  .review-panel label {
    display: grid;
    gap: 0.32rem;
    margin-top: 0.7rem;
  }

  .review-panel label > span {
    color: var(--color-ink-muted);
    font-size: 0.54rem;
    font-weight: 700;
  }

  .review-panel label i {
    font-weight: 500;
  }

  .review-panel textarea {
    width: 100%;
    resize: vertical;
    border: 1px solid var(--color-rule);
    padding: 0.55rem;
    outline: none;
    background: var(--color-surface);
    color: var(--color-ink);
    font: inherit;
    font-size: 0.62rem;
    line-height: 1.5;
  }

  .review-panel textarea:focus {
    border-color: var(--color-focus);
    box-shadow: 0 0 0 2px var(--color-focus-muted);
  }

  .review-panel > div {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.35rem;
    margin-top: 0.55rem;
  }

  .review-panel > div button {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.5rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.54rem;
  }

  .review-panel > div button.approve {
    border-color: var(--color-lichen);
    background: var(--color-lichen);
    color: var(--color-surface);
  }

  .review-panel > div button:disabled {
    cursor: wait;
    opacity: 0.65;
  }

  .review-note {
    border-left: 2px solid var(--color-lichen);
    padding: 0.65rem 0.75rem;
    background: var(--color-lichen-muted);
  }

  .review-note p {
    margin: 0 0 0.25rem;
    color: var(--color-lichen);
    font-size: 0.53rem;
    font-weight: 720;
    text-transform: uppercase;
  }

  .review-note blockquote {
    margin: 0;
    color: var(--color-ink-soft);
    font-family: var(--font-editorial);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .detail-section {
    display: grid;
    gap: 0.55rem;
    border-top: 1px solid var(--color-rule);
    padding-top: 0.85rem;
  }

  .detail-section > header {
    flex-direction: column;
    gap: 0.1rem;
  }

  .version-list,
  .source-list {
    display: grid;
    gap: 1px;
    margin: 0;
    padding: 0;
    background: var(--color-rule);
    list-style: none;
  }

  .version-list li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: 0.55rem;
    padding: 0.6rem;
    background: var(--color-surface);
  }

  .version-list li.version--current {
    box-shadow: inset 2px 0 var(--color-lichen);
  }

  .version-list li > span:first-child {
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
    font-weight: 750;
  }

  .version-list li > div {
    min-width: 0;
    display: grid;
    gap: 0.08rem;
  }

  .version-list strong {
    font-size: 0.61rem;
  }

  .version-list small,
  .version-list li > span:last-child {
    color: var(--color-ink-muted);
    font-size: 0.5rem;
  }

  .version-list p {
    margin: 0.2rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.56rem;
    line-height: 1.4;
  }

  .source-list li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: 0.55rem;
    padding: 0.6rem;
    background: var(--color-surface);
  }

  .source-list li > span {
    width: 1.8rem;
    height: 1.8rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    color: var(--color-clay);
  }

  .source-list li > div {
    min-width: 0;
    display: grid;
    gap: 0.1rem;
  }

  .source-list strong {
    overflow-wrap: anywhere;
    font-size: 0.62rem;
  }

  .source-list small,
  .source-list p {
    color: var(--color-ink-muted);
    font-size: 0.52rem;
  }

  .source-list p {
    margin: 0.25rem 0 0;
    line-height: 1.45;
  }

  .source-list li > a {
    color: var(--color-clay);
  }

  .evidence-list {
    display: grid;
    gap: 0.55rem;
  }

  .evidence-list > article {
    border: 1px solid var(--color-rule);
    padding: 0.65rem;
    background: var(--color-surface);
  }

  .evidence-list > article > header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .evidence-list > article > header span {
    padding: 0.2rem 0.35rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.48rem;
    font-weight: 720;
  }

  .evidence-list > article > header span.evidence--used {
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .evidence-list > article > header small {
    color: var(--color-ink-muted);
    font-size: 0.5rem;
  }

  .evidence-list h5 {
    margin: 0.55rem 0 0;
    font-family: var(--font-editorial);
    font-size: 0.73rem;
    font-weight: 560;
    line-height: 1.45;
  }

  .evidence-list blockquote {
    margin: 0.5rem 0 0;
    border-left: 1px solid var(--color-tobacco);
    padding-left: 0.55rem;
    color: var(--color-ink-muted);
    font-family: var(--font-editorial);
    font-size: 0.63rem;
    line-height: 1.5;
  }

  .evidence-list > article > p {
    margin: 0.5rem 0;
    color: var(--color-ink-muted);
    font-size: 0.52rem;
  }

  .evidence-list a {
    color: var(--color-clay);
  }

  .section-note {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .artifact-loading,
  .detail-loading {
    display: grid;
    gap: 0.65rem;
  }

  .artifact-loading {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .artifact-loading span,
  .detail-loading span {
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

  .detail-loading span:nth-child(2) {
    height: 18rem;
  }

  .detail-loading span:nth-child(3) {
    height: 8rem;
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

  @media (max-width: 25rem) {
    .artifact-grid,
    .artifact-loading {
      grid-template-columns: 1fr;
    }
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }
</style>
