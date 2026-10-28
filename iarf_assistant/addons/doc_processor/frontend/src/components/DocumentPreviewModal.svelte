<script lang="ts">
  import { Icon, withEpubReviewAnchors } from "@carlina/ui";
  import { createEventDispatcher, onDestroy, tick } from "svelte";
  import { getDocument, getSourceFileUrl, reextractDocumentMetadata, updateDocumentContent, updateDocumentMetadata } from "../lib/api";
  import { applyContentPatches, changedContentPatches, editableSemanticBlocks, editorHtmlFromDocument, headingNavigationTargets, previewHtmlWithListMarkers, scrollHeadingInPreview, structuralChangeSummary, validateContentPatches, type EditableSemanticBlock, type HeadingNavigationTarget } from "../lib/semanticHtml";
  import type { CanonicalDocumentMetadata, ProcessedDocument } from "../lib/types";
  import PageFurnitureCleanup from "./PageFurnitureCleanup.svelte";
  import PageBoundaryRepair from "./PageBoundaryRepair.svelte";
  import BaseModal from "./BaseModal.svelte";
  import MetadataPanel from "./MetadataPanel.svelte";
  import ExtractionIssues from "./ExtractionIssues.svelte";

  export let open = false;
  export let documentId: string | null = null;
  export let initialAnchor = "";
  const dispatch = createEventDispatcher<{ close: void; saved: void; export: string }>();

  let lastDocumentId: string | null = null;
  let documentPayload: ProcessedDocument | null = null;
  let loading = false;
  let saving = false;
  let metadataSaving = false;
  let metadataReextracting = false;
  let error = "";
  let activeTab: "html" | "md" | "metadata" | "issues" = "html";
  let editing = false;
  let originalHtml = "";
  let blocks: EditableSemanticBlock[] = [];
  let showToc = true;
  let compareWithPdf = false;
  let controlsOpen = false;
  let loadGeneration = 0;
  let metadataRequestGeneration = 0;
  let loadAbortController: AbortController | null = null;
  let saveConfirmationOpen = false;
  let previewPane: HTMLDivElement | null = null;

  $: if (open && documentId && documentId !== lastDocumentId) {
    lastDocumentId = documentId;
    void load(documentId);
  }
  $: if (!open) {
    loadAbortController?.abort();
    lastDocumentId = null;
    editing = false;
    blocks = [];
    compareWithPdf = false;
    controlsOpen = false;
    saveConfirmationOpen = false;
    metadataSaving = false;
    metadataReextracting = false;
  }
  $: previewHtml = withEpubReviewAnchors(previewHtmlWithListMarkers(documentPayload?.preview_html || originalHtml || "<p>No HTML output available.</p>"));
  $: if (open && !loading && documentPayload && previewPane && initialAnchor) {
    const target = initialAnchor;
    void tick().then(() => {
      const element = previewPane?.querySelector<HTMLElement>(`[id="${CSS.escape(target)}"]`);
      element?.scrollIntoView({ block: "center" });
      if (element) { element.tabIndex = -1; element.focus({ preventScroll: true }); }
    });
  }
  $: markdown = documentPayload?.output_markdown || "No Markdown output available.";
  $: isPdfSource = Boolean(documentPayload?.source_available && documentPayload?.filename?.toLowerCase().endsWith(".pdf"));
  $: pdfUrl = documentId && isPdfSource ? getSourceFileUrl(documentId, "inline") : "";
  $: downloadUrl = documentId && isPdfSource ? getSourceFileUrl(documentId, "attachment") : "";
  $: if (!isPdfSource && compareWithPdf) compareWithPdf = false;
  $: tocItems = headingNavigationTargets(originalHtml)
    .filter((heading) => ["h1", "h2", "h3", "h4"].includes(heading.tag));
  $: contentPatches = editing ? changedContentPatches(originalHtml, blocks) : [];

  onDestroy(() => loadAbortController?.abort());

  async function load(id: string) {
    metadataRequestGeneration += 1;
    const generation = ++loadGeneration;
    loadAbortController?.abort();
    const controller = new AbortController();
    loadAbortController = controller;
    loading = true;
    error = "";
    editing = false;
    compareWithPdf = false;
    activeTab = "html";
    controlsOpen = false;
    blocks = [];
    documentPayload = null;
    try {
      const nextDocument = await getDocument(id, controller.signal);
      if (controller.signal.aborted || generation !== loadGeneration || id !== documentId) return;
      documentPayload = nextDocument;
      originalHtml = editorHtmlFromDocument(nextDocument);
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError") && generation === loadGeneration) {
        error = err instanceof Error ? err.message : "Failed to load document preview.";
      }
    } finally {
      if (generation === loadGeneration) loading = false;
    }
  }

  async function refreshAfterCleanup() {
    if (!documentId) return false;
    const id = documentId;
    const generation = loadGeneration;
    try {
      const next = await getDocument(id);
      if (!open || documentId !== id || generation !== loadGeneration) return false;
      documentPayload = next;
      originalHtml = editorHtmlFromDocument(next);
      dispatch("saved");
      return true;
    } catch (err) {
      if (documentId === id) error = err instanceof Error ? err.message : "Reload failed.";
      return false;
    }
  }

  async function returnFromIssues() {
    if (await refreshAfterCleanup()) activeTab = "html";
  }

  function startEditing() {
    editing = true;
    activeTab = "html";
    blocks = editableSemanticBlocks(originalHtml);
  }

  function selectTab(tab: "html" | "md" | "metadata") {
    activeTab = tab;
    if (tab === "metadata") {
      compareWithPdf = false;
    }
  }
  function cancelEditing() {
    editing = false;
    blocks = [];
    saveConfirmationOpen = false;
  }
  function updateBlock(index: number, html: string) {
    blocks[index] = { ...blocks[index], html };
    blocks = [...blocks];
  }
  async function save() {
    if (!documentId) return;
    saving = true;
    error = "";
    try {
      const validationError = validateContentPatches(contentPatches);
      if (validationError) {
        error = validationError;
        saveConfirmationOpen = false;
        return;
      }
      // The no-change path is originalHtml byte-for-byte.
      const editedHtml = applyContentPatches(originalHtml, contentPatches);
      documentPayload = await updateDocumentContent(documentId, editedHtml);
      originalHtml = documentPayload.output_html || editedHtml;
      editing = false;
      blocks = [];
      saveConfirmationOpen = false;
      dispatch("saved");
    } catch (err) {
      error = err instanceof Error ? err.message : "Failed to save edits.";
    } finally {
      saving = false;
    }
  }
  async function saveMetadata(metadata: CanonicalDocumentMetadata) {
    if (!documentId) return;
    const targetId = documentId;
    const expectedUpdatedAt = documentPayload?.updated_at;
    const generation = ++metadataRequestGeneration;
    metadataSaving = true;
    error = "";
    try {
      const nextDocument = await updateDocumentMetadata(targetId, metadata, expectedUpdatedAt);
      if (!open || documentId !== targetId || generation !== metadataRequestGeneration) return;
      documentPayload = nextDocument;
      originalHtml = editorHtmlFromDocument(nextDocument);
      dispatch("saved");
    } catch (err) {
      if (open && documentId === targetId && generation === metadataRequestGeneration) {
        error = err instanceof Error ? err.message : "Failed to save metadata.";
      }
    } finally {
      if (documentId === targetId && generation === metadataRequestGeneration) metadataSaving = false;
    }
  }
  async function reextractMetadata() {
    if (!documentId) return;
    const targetId = documentId;
    const expectedUpdatedAt = documentPayload?.updated_at;
    const generation = ++metadataRequestGeneration;
    metadataReextracting = true;
    error = "";
    try {
      const nextDocument = await reextractDocumentMetadata(targetId, expectedUpdatedAt);
      if (!open || documentId !== targetId || generation !== metadataRequestGeneration) return;
      documentPayload = nextDocument;
      originalHtml = editorHtmlFromDocument(nextDocument);
      dispatch("saved");
    } catch (err) {
      if (open && documentId === targetId && generation === metadataRequestGeneration) {
        error = err instanceof Error ? err.message : "Failed to refresh metadata from the source.";
      }
    } finally {
      if (documentId === targetId && generation === metadataRequestGeneration) metadataReextracting = false;
    }
  }
  function scrollToHeading(target: HeadingNavigationTarget) {
    if (!previewPane) return;
    scrollHeadingInPreview(previewPane, target);
  }
</script>

<BaseModal {open} title={documentPayload?.filename || "Extracted Output"} size="full" on:close={() => dispatch("close")}>
  <svelte:fragment slot="header-actions">
  <button class="button ghost" type="button" disabled={loading || editing || documentPayload?.status !== "completed"} aria-pressed={activeTab === "issues"} on:click={() => { if (activeTab === "issues") void returnFromIssues(); else activeTab = "issues"; controlsOpen = false; }}>Review issues</button>
  <button
    class="dp-icon-btn dp-preview-controls-toggle"
    class:active={controlsOpen}
    type="button"
    aria-expanded={controlsOpen}
    aria-controls="dp-preview-controls"
    aria-label={controlsOpen ? "Hide preview controls" : "Show preview controls"}
    title={controlsOpen ? "Hide preview controls" : "Show preview controls"}
    on:click={() => (controlsOpen = !controlsOpen)}
  >
    <Icon name="settings" size={16} />
  </button>
  </svelte:fragment>
  <div class="dp-preview-modal">
    {#if error}<div class="dp-notice error" role="alert">{error}</div>{/if}
    {#if controlsOpen}
      <section id="dp-preview-controls" class="dp-preview-controls" aria-label="Preview controls">
        <div class="dp-editor-toolbar dp-preview-action-row">
          {#if editing}
            <button class="dp-icon-btn success" type="button" disabled={saving} aria-label="Save edits" title="Save edits" on:click={() => (saveConfirmationOpen = true)}>
              <Icon name={saving ? "refresh" : "check"} size={16} class={saving ? "dp-spin" : ""} />
            </button>
            <button class="dp-icon-btn" type="button" disabled={saving} aria-label="Cancel editing" title="Cancel editing" on:click={cancelEditing}><Icon name="close" size={16} /></button>
          {:else if activeTab !== "metadata"}
            <button class="dp-icon-btn" type="button" disabled={loading} aria-label="Edit extracted text" title="Edit extracted text safely" on:click={startEditing}><Icon name="edit" size={16} /></button>
          {/if}
          {#if activeTab !== "metadata"}
            <button class="dp-icon-btn" class:active={showToc && !compareWithPdf && !editing} type="button" disabled={compareWithPdf || editing} aria-label="Toggle document outline" aria-pressed={showToc && !compareWithPdf && !editing} title="Toggle document outline" on:click={() => (showToc = !showToc)}><Icon name="logs" size={16} /></button>
          {/if}
          {#if isPdfSource && activeTab !== "metadata"}
            <button class="dp-icon-btn" class:active={compareWithPdf} type="button" disabled={loading || editing} aria-label="Compare PDF and extracted text" aria-pressed={compareWithPdf} title="Show source PDF next to extracted text" on:click={() => (compareWithPdf = !compareWithPdf)}><Icon name="workspaces" size={16} /></button>
          {/if}
          {#if documentId && activeTab !== "metadata" && documentPayload?.status === "completed"}
            <PageFurnitureCleanup {documentId} compact disabled={loading || editing || saving} on:saved={refreshAfterCleanup} />
            <PageBoundaryRepair {documentId} compact disabled={loading || editing || saving} on:saved={refreshAfterCleanup} />
          {/if}
        </div>
        {#if editing}<p class="dp-muted" id="dp-semantic-edit-help">Only text-node corrections are allowed. Inline elements and attributes, lists, figures, templates, IDs, links, endnotes, element tags, and ordering are protected.</p>{/if}
        <div class="dp-tabs" role="tablist" aria-label="Document preview views"><button class:active={activeTab === "html"} type="button" role="tab" aria-selected={activeTab === "html"} on:click={() => selectTab("html")}>Rendered Preview</button><button class:active={activeTab === "md"} type="button" role="tab" aria-selected={activeTab === "md"} disabled={editing} on:click={() => selectTab("md")}>Raw Markdown</button><button class:active={activeTab === "metadata"} type="button" role="tab" aria-selected={activeTab === "metadata"} disabled={editing} on:click={() => selectTab("metadata")}>Metadata</button></div>
      </section>
    {/if}
    {#if loading}
      <div class="dp-empty-state"><Icon name="refresh" size={18} class="dp-spin" /><span>Loading preview...</span></div>
    {:else}
      {#if documentPayload?.preview_warning && activeTab === "html" && !editing}<p class="dp-alert" role="status">{documentPayload.preview_warning}</p>{/if}
      {#if activeTab === "issues" && documentId}
        {#key documentId}<ExtractionIssues {documentId} {pdfUrl} on:preview={returnFromIssues} />{/key}
      {:else if activeTab === "metadata"}
        <MetadataPanel document={documentPayload} saving={metadataSaving} reextracting={metadataReextracting} on:save={(event) => saveMetadata(event.detail)} on:reextract={reextractMetadata} />
      {:else}
      <div class="dp-preview-layout" class:has-toc={showToc && activeTab === "html" && !compareWithPdf && !editing} class:compare={compareWithPdf}>
        {#if showToc && activeTab === "html" && !compareWithPdf && !editing}
          <aside class="dp-preview-toc"><h4>Document Outline</h4>
              {#if tocItems.length === 0}<small class="dp-muted">No headings found.</small>{:else}<nav class="dp-toc-nav">{#each tocItems as item (item.key)}<button class="dp-toc-item {item.tag}" type="button" on:click={() => scrollToHeading(item)}>{item.text || "Untitled Heading"}</button>{/each}</nav>{/if}
          </aside>
        {/if}
        {#if compareWithPdf}
          <section class="dp-compare-pdf-pane" aria-label="Original PDF"><header><strong><Icon name="file" size={16} /> Original PDF</strong>{#if downloadUrl}<a class="dp-icon-btn" href={downloadUrl} download={documentPayload?.filename || "document.pdf"} aria-label="Download original PDF" title="Download original PDF"><Icon name="download" size={16} /></a>{/if}</header>
            {#if pdfUrl}<iframe class="dp-compare-pdf-frame" src={pdfUrl} title={`PDF source - ${documentPayload?.filename || "document"}`} loading="lazy" allowfullscreen></iframe>{:else}<div class="dp-empty-state"><Icon name="file" size={20} /><span>No source PDF available.</span></div>{/if}
          </section>
        {/if}
        <div class="dp-preview-content-pane" class:compare={compareWithPdf}>
          {#if compareWithPdf}<header class="dp-compare-text-header"><strong>{editing ? "Semantic text editor" : activeTab === "md" ? "Extracted Markdown" : "Extracted Text"}</strong>{#if documentId}<button class="dp-icon-btn" type="button" disabled={editing || saving} aria-label="Export extracted document" title={editing || saving ? "Save edits before exporting" : "Export extracted document"} on:click={() => dispatch("export", documentId)}><Icon name="arrow-up-right" size={16} /></button>{/if}</header>{/if}
          {#if !editing}
            <div bind:this={previewPane} class:active={activeTab === "html"} class="dp-preview-pane html">{@html previewHtml}</div>
          {:else}
            <div class:active={activeTab === "html"} class="dp-preview-pane html editing-blocks" aria-describedby="dp-semantic-edit-help"><div class="dp-blocks-list">
              {#if !blocks.length}<p class="dp-muted">No independently editable text blocks were found. The semantic preview remains read-only.</p>{/if}
              {#each blocks as block, index (block.id)}<label class="dp-block-item" data-tag={block.tag}><span class="dp-block-tag-badge">{block.tag}</span><textarea aria-label={`Edit ${block.tag} content`} value={block.html} on:input={(event) => updateBlock(index, event.currentTarget.value)}></textarea></label>{/each}
            </div></div>
          {/if}
          <div class:active={activeTab === "md"} class="dp-preview-pane md"><pre>{markdown}</pre></div>
        </div>
      </div>
      {/if}
    {/if}
  </div>
  {#if saveConfirmationOpen}
    <section class="dp-semantic-save-confirmation" aria-live="polite"><strong>Review changes before saving</strong><p>{contentPatches.length} text/inline content {contentPatches.length === 1 ? "change" : "changes"} will be applied to the original document tree.</p><p>{structuralChangeSummary()}</p><div class="dp-modal-footer"><button class="button ghost" type="button" disabled={saving} on:click={() => (saveConfirmationOpen = false)}>Back to editing</button><button class="button success" type="button" disabled={saving} on:click={save}>Confirm and save</button></div></section>
  {/if}
</BaseModal>
