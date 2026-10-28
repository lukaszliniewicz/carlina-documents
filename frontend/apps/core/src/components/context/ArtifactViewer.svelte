<script lang="ts">
  import type {
    FrontendApiClient,
    FrontendArtifactContent,
    FrontendArtifactDetail,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import {
    resolveArtifactViewer,
    trustedArtifactAssetUrl,
  } from "../../lib/workspace-state.ts";
  import StructuredData from "./StructuredData.svelte";

  interface Props {
    api: FrontendApiClient;
    detail: FrontendArtifactDetail;
  }

  let { api, detail }: Props = $props();

  let content = $state<FrontendArtifactContent["content"] | null>(null);
  let loading = $state(false);
  let error = $state("");

  const viewer = $derived(resolveArtifactViewer(detail.artifact));
  const assetUrl = $derived(
    trustedArtifactAssetUrl(
      detail.artifact.download_url,
      typeof window !== "undefined"
        ? window.location.origin
        : "https://iarf.local",
    ),
  );
  const structuredContent = $derived.by(() => {
    if (viewer.id !== "structured" || !content?.content) return null;
    try {
      return JSON.parse(content.content) as unknown;
    } catch {
      return content.content;
    }
  });

  $effect(() => {
    const artifactId = detail.artifact.id;
    const contentUrl = detail.artifact.content_url;
    const needsContent = viewer.needsContent;
    content = null;
    error = "";
    if (!needsContent) {
      loading = false;
      return;
    }
    const controller = new AbortController();
    loading = true;
    void api
      .request<FrontendArtifactContent>(contentUrl, {
        signal: controller.signal,
      })
      .then((payload) => {
        if (artifactId !== detail.artifact.id) return;
        content = payload.content;
        if (!payload.content.available) {
          error =
            payload.content.error ??
            "A text preview is not available for this artifact.";
        }
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        error =
          reason instanceof Error
            ? reason.message
            : "The artifact preview could not be loaded.";
      })
      .finally(() => {
        if (!controller.signal.aborted) loading = false;
      });
    return () => controller.abort();
  });
</script>

<section class="viewer" aria-label={`${viewer.label} preview`}>
  <header>
    <span>{viewer.label} preview</span>
    {#if content?.truncated}<i>Preview truncated</i>{/if}
  </header>

  {#if loading}
    <div class="viewer-loading" aria-busy="true" aria-label="Loading preview">
      <span></span><span></span><span></span>
    </div>
  {:else if viewer.id === "image" && assetUrl}
    <div class="media-preview">
      <img src={assetUrl} alt={detail.artifact.label} />
    </div>
  {:else if viewer.id === "audio" && assetUrl}
    <div class="media-preview media-preview--audio">
      <Icon name="volume" size={28} />
      <audio controls preload="metadata" src={assetUrl}>
        <track kind="captions" />
      </audio>
    </div>
  {:else if viewer.id === "video" && assetUrl}
    <div class="media-preview">
      <video controls preload="metadata" src={assetUrl}>
        <track kind="captions" />
      </video>
    </div>
  {:else if viewer.id === "pdf" && assetUrl}
    <div class="pdf-preview">
      <object
        data={assetUrl}
        type="application/pdf"
        title={`Preview of ${detail.artifact.label}`}
      >
        <p>
          This browser cannot embed the PDF.
          <a href={assetUrl}>Open the document</a>.
        </p>
      </object>
    </div>
  {:else if
    (viewer.id === "markdown" || viewer.id === "html") &&
    content?.rendered_html}
    <article class="rendered-content prose">
      {@html content.rendered_html}
    </article>
  {:else if viewer.id === "structured" && structuredContent !== null}
    <div class="structured-preview">
      <StructuredData value={structuredContent} maxRows={120} />
    </div>
  {:else if viewer.id === "text" && content?.content}
    <article class="text-preview">{content.content}</article>
  {:else}
    <div class="preview-unavailable">
      <span><Icon name="file" size={21} /></span>
      <h4>Preview unavailable</h4>
      <p>
        {error ||
          "This file type is kept intact and can be opened with its native application."}
      </p>
      {#if assetUrl}
        <a href={assetUrl}>
          <Icon name="download" size={14} /> Download artifact
        </a>
      {/if}
    </div>
  {/if}
</section>

<style>
  .viewer {
    min-width: 0;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .viewer > header {
    min-height: 2.2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0 0.65rem;
    color: var(--color-ink-muted);
    font-size: 0.55rem;
    font-weight: 720;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .viewer > header i {
    color: var(--color-tobacco);
    font-size: 0.5rem;
    font-style: normal;
    letter-spacing: 0;
    text-transform: none;
  }

  .viewer-loading {
    min-height: 12rem;
    display: grid;
    align-content: center;
    gap: 0.5rem;
    padding: 1rem;
  }

  .viewer-loading span {
    height: 0.65rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface-sunken),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: loading 1.4s infinite linear;
  }

  .viewer-loading span:nth-child(2) {
    width: 82%;
  }

  .viewer-loading span:nth-child(3) {
    width: 64%;
  }

  .media-preview {
    min-height: 12rem;
    display: grid;
    place-items: center;
    padding: 0.75rem;
    background:
      linear-gradient(var(--color-rule) 1px, transparent 1px),
      linear-gradient(90deg, var(--color-rule) 1px, transparent 1px),
      var(--color-paper);
    background-size: 18px 18px;
  }

  .media-preview img,
  .media-preview video {
    max-width: 100%;
    max-height: min(52vh, 32rem);
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .media-preview--audio {
    min-height: 8rem;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.75rem;
    padding: 1rem;
    color: var(--color-clay);
  }

  .media-preview audio {
    width: 100%;
  }

  .pdf-preview object {
    width: 100%;
    height: min(55vh, 36rem);
    display: block;
    border: 0;
  }

  .rendered-content,
  .text-preview,
  .structured-preview {
    max-height: min(55vh, 36rem);
    overflow: auto;
    padding: 0.9rem;
  }

  .rendered-content {
    color: var(--color-ink-soft);
    font-family: var(--font-editorial);
    font-size: 0.72rem;
    line-height: 1.65;
  }

  .rendered-content :global(h1),
  .rendered-content :global(h2),
  .rendered-content :global(h3) {
    font-family: var(--font-editorial);
    font-weight: 560;
  }

  .rendered-content :global(img) {
    max-width: 100%;
  }

  .text-preview {
    overflow-wrap: anywhere;
    color: var(--color-ink-soft);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.62rem;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  .preview-unavailable {
    min-height: 12rem;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.55rem;
    padding: 1rem;
  }

  .preview-unavailable > span {
    width: 2.2rem;
    height: 2.2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
  }

  .preview-unavailable h4 {
    margin: 0.15rem 0 0;
    font-family: var(--font-editorial);
    font-size: 0.95rem;
    font-weight: 560;
  }

  .preview-unavailable p {
    max-width: 25rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    line-height: 1.5;
  }

  .preview-unavailable a {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--color-rule-strong);
    margin-top: 0.25rem;
    padding: 0 0.55rem;
    color: var(--color-ink-soft);
    font-size: 0.58rem;
    font-weight: 700;
    text-decoration: none;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }
</style>
