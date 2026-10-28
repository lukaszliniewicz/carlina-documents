<script lang="ts">
  import { EmptyState, Icon, SearchField } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";
  import type { ProcessedDocument } from "../lib/types";
  import { isActiveDocumentStatus } from "../lib/utils";
  import DocumentCard from "./DocumentCard.svelte";

  export let documents: ProcessedDocument[] = [];
  export let loading = false;

  const dispatch = createEventDispatcher<{
    crop: string;
    reviewImages: string;
    preview: string;
    pdfPreview: string;
    export: string;
    delete: string;
    resume: string;
    pause: string;
    stop: string;
  }>();

  let query = "";
  let statusFilter = "all";

  function matchesStatus(document: ProcessedDocument, filter: string): boolean {
    if (filter === "active") return isActiveDocumentStatus(document.status);
    if (filter === "ready") return ["completed", "completed_with_errors"].includes(document.status);
    if (filter === "review") return ["error", "needs_cropping", "reviewing_images", "paused"].includes(document.status);
    return true;
  }

  $: normalizedQuery = query.trim().toLocaleLowerCase();
  $: filteredDocuments = documents.filter((document) => {
    const metadata = document.metadata;
    const searchable = [
      document.filename,
      metadata?.title,
      metadata?.author,
      document.status
    ].filter(Boolean).join(" ").toLocaleLowerCase();
    return matchesStatus(document, statusFilter) && (!normalizedQuery || searchable.includes(normalizedQuery));
  });
</script>

<section class="dp-content-panel">
  <header class="dp-content-header">
    <div>
      <p class="dp-eyebrow">Library</p>
      <h2>Processed documents</h2>
      <p>Review source state, extraction progress, and publication-ready output.</p>
    </div>
    <span class="dp-library-count">{filteredDocuments.length} of {documents.length}</span>
  </header>

  <div class="dp-library-tools">
    <SearchField
      id="doc-processor-library-search"
      value={query}
      label="Search documents"
      placeholder="Search filename, title, author, or status"
      oninput={(value) => (query = value)}
    />
    <label>
      <span>Status</span>
      <select bind:value={statusFilter}>
        <option value="all">All documents</option>
        <option value="active">Processing</option>
        <option value="ready">Ready</option>
        <option value="review">Needs review</option>
      </select>
    </label>
  </div>

  {#if loading}
    <div class="dp-loading-state">
      <Icon name="refresh" size={20} class="dp-spin" />
      <span>Loading document library…</span>
    </div>
  {:else if !documents.length}
    <EmptyState
      icon="file"
      title="No documents yet"
      description="Upload a source document from the processing panel to begin extraction."
    />
  {:else if !filteredDocuments.length}
    <EmptyState
      icon="search"
      title="No matching documents"
      description="Change the search term or status filter to widen the library view."
    />
  {:else}
    <div class="dp-document-list">
      {#each filteredDocuments as document (document.id)}
        <DocumentCard
          {document}
          on:crop={(event) => dispatch("crop", event.detail)}
          on:reviewImages={(event) => dispatch("reviewImages", event.detail)}
          on:preview={(event) => dispatch("preview", event.detail)}
          on:pdfPreview={(event) => dispatch("pdfPreview", event.detail)}
          on:export={(event) => dispatch("export", event.detail)}
          on:delete={(event) => dispatch("delete", event.detail)}
          on:resume={(event) => dispatch("resume", event.detail)}
          on:pause={(event) => dispatch("pause", event.detail)}
          on:stop={(event) => dispatch("stop", event.detail)}
        />
      {/each}
    </div>
  {/if}
</section>
