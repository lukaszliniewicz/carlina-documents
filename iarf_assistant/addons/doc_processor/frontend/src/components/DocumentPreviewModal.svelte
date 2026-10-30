<script lang="ts">
  import { Icon, confirmDiscardChanges, withEpubReviewAnchors } from "@carlina/ui";
  import { createEventDispatcher, onDestroy, tick } from "svelte";
  import { getDocument, getSourceFileUrl, reextractDocumentMetadata, updateDocumentContent, updateDocumentMetadata } from "../lib/api";
  import { applyContentPatches, editableSemanticBlocks, editorHtmlFromDocument, headingNavigationTargets, previewHtmlWithListMarkers, scrollHeadingInPreview, structuralChangeSummary, validateContentPatches, type EditableSemanticBlock, type HeadingNavigationTarget, type SemanticContentPatch } from "../lib/semanticHtml";
  import type { CanonicalDocumentMetadata, ProcessedDocument } from "../lib/types";
  import PageFurnitureCleanup from "./PageFurnitureCleanup.svelte";
  import PageBoundaryRepair from "./PageBoundaryRepair.svelte";
  import BaseModal from "./BaseModal.svelte";
  import MetadataPanel from "./MetadataPanel.svelte";
  import ExtractionIssues from "./ExtractionIssues.svelte";
  import SemanticTextEditor from "./SemanticTextEditor.svelte";
  import PdfSourceViewer from "./PdfSourceViewer.svelte";

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
  let originalBlocks = new Map<string, EditableSemanticBlock>();
  let patches = new Map<string, SemanticContentPatch>();
  const editorPageSize = 30;
  let editorPage = 0;
  let metadataDirty = false;
  let saveConflict = false;
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
    if (lastDocumentId) loadGeneration += 1;
    lastDocumentId = null;
    editing = false;
    blocks = [];
    patches = new Map();
    metadataDirty = false;
    saving = false;
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
  $: contentPatches = editing ? Array.from(patches.values()) : [];
  $: visibleBlocks = blocks.slice(editorPage * editorPageSize, (editorPage + 1) * editorPageSize);
  $: busy = saving || metadataSaving || metadataReextracting;
  $: dirty = contentPatches.length > 0 || metadataDirty;

  onDestroy(() => loadAbortController?.abort());

  async function load(id: string) {
    metadataRequestGeneration += 1;
    const generation = ++loadGeneration;
    loadAbortController?.abort();
    const controller = new AbortController();
    loadAbortController = controller;
    loading = true;
    saving = false;
    saveConfirmationOpen = false;
    error = "";
    editing = false;
    compareWithPdf = false;
    activeTab = "html";
    controlsOpen = false;
    blocks = [];
    patches = new Map();
    metadataDirty = false;
    saveConflict = false;
    documentPayload = null;
    originalHtml = "";
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

  async function startEditing() {
    if (!await confirmDiscardChanges({ dirty: metadataDirty, busy })) return;
    editing = true;
    controlsOpen = true;
    activeTab = "html";
    metadataDirty = false;
    blocks = editableSemanticBlocks(originalHtml);
    originalBlocks = new Map(blocks.map(block => [block.id, block]));
    patches = new Map();
    editorPage = 0;
    saveConflict = false;
  }

  async function selectTab(tab: "html" | "md" | "metadata") {
    if (tab === activeTab) return;
    if (activeTab === "metadata" && tab !== "metadata" && !await confirmDiscardChanges({ dirty: metadataDirty, busy })) return;
    metadataDirty = false;
    activeTab = tab;
    if (tab === "metadata") {
      compareWithPdf = false;
    }
  }
  async function cancelEditing() {
    if (!await confirmDiscardChanges({ dirty: contentPatches.length > 0, busy })) return;
    editing = false;
    blocks = [];
    patches = new Map();
    saveConfirmationOpen = false;
  }
  function updateBlock(index: number, html: string) {
    const block = blocks[index];
    const original = originalBlocks.get(block.id)!;
    const next = new Map(patches);
    if (html === original.html) next.delete(block.id);
    else next.set(block.id, { kind: "content", path: original.path, tag: original.tag, originalHtml: original.html, html });
    patches = next;
    blocks[index] = { ...block, html };
    blocks = [...blocks];
  }
  async function reloadDocument() {
    if (documentId && await confirmDiscardChanges({ dirty, busy })) void load(documentId);
  }
  async function showIssues() {
    if (!await confirmDiscardChanges({ dirty: metadataDirty, busy })) return;
    metadataDirty = false;
    if (activeTab === "issues") void returnFromIssues(); else activeTab = "issues";
    controlsOpen = false;
  }
  async function save() {
    if (!documentId || saving || saveConflict) return;
    const targetId = documentId;
    const generation = loadGeneration;
    const expectedRevision = documentPayload?.updated_at;
    if (!expectedRevision) { error = "Reload the document before saving corrections."; saveConflict = true; return; }
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
      const nextDocument = await updateDocumentContent(targetId, editedHtml, expectedRevision);
      if (!open || targetId !== documentId || generation !== loadGeneration) return;
      documentPayload = nextDocument;
      originalHtml = documentPayload.output_html || editedHtml;
      editing = false;
      blocks = [];
      patches = new Map();
      saveConfirmationOpen = false;
      dispatch("saved");
    } catch (err) {
      if (open && targetId === documentId && generation === loadGeneration) {
        saveConfirmationOpen = false;
        error = err instanceof Error ? err.message : "Failed to save edits.";
        saveConflict = err instanceof Error && "status" in err && [409, 428].includes(Number(err.status));
      }
    } finally {
      if (targetId === documentId && generation === loadGeneration) saving = false;
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

<BaseModal {open} {dirty} {busy} title={documentPayload?.filename || "Extracted Output"} size="full" on:close={() => dispatch("close")}>
  <svelte:fragment slot="header-actions">
  <button class="button" type="button" disabled={loading || editing || busy} on:click={startEditing}><Icon name="edit" size={16} /> Correct text</button>
  {#if isPdfSource}<button class="button" type="button" disabled={loading || editing || activeTab === "metadata" || busy} aria-pressed={compareWithPdf} on:click={() => compareWithPdf = !compareWithPdf}><Icon name="workspaces" size={16} /> Compare PDF</button>{/if}
  {#if documentId}<button class="button" type="button" disabled={loading || editing || dirty || busy} on:click={() => documentId && dispatch("export", documentId)}><Icon name="download" size={16} /> Export</button>{/if}
  <button class="button ghost" type="button" disabled={loading || editing || busy || documentPayload?.status !== "completed"} aria-pressed={activeTab === "issues"} on:click={showIssues}>Review issues</button>
  <button
    class="dp-icon-btn dp-preview-controls-toggle"
    class:active={controlsOpen}
    type="button"
    aria-expanded={controlsOpen}
    aria-controls="dp-preview-controls"
    disabled={editing || busy}
    aria-label={controlsOpen ? "Hide preview controls" : "Show preview controls"}
    title={controlsOpen ? "Hide preview controls" : "Show preview controls"}
    on:click={() => (controlsOpen = !controlsOpen)}
  >
    <Icon name="settings" size={16} />
  </button>
  </svelte:fragment>
  <div class="dp-preview-modal">
    {#if error}<div class="dp-notice error" role="alert">{error} {#if saveConflict}<button class="button" type="button" disabled={busy} on:click={reloadDocument}>Reload document</button><span>Your corrections are still in the editor. Copy anything you want to keep before reloading.</span>{/if}</div>{/if}
    {#if controlsOpen}
      <section id="dp-preview-controls" class="dp-preview-controls" aria-label="Preview controls">
        <div class="dp-editor-toolbar dp-preview-action-row">
          {#if editing}
            <button class="button success" type="button" disabled={saving || saveConflict || !contentPatches.length} aria-label="Save edits" title="Save edits" on:click={() => (saveConfirmationOpen = true)}>
              <Icon name={saving ? "refresh" : "check"} size={16} class={saving ? "dp-spin" : ""} /> Save corrections
            </button>
            <button class="button" type="button" disabled={saving} aria-label="Cancel editing" title="Cancel editing" on:click={cancelEditing}><Icon name="close" size={16} /> Cancel editing</button>
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
        {#if editing}<p class="dp-muted" id="dp-semantic-edit-help">Correct the text below. Formatting, links, notes, tables, images and their order stay intact. Formatted passages have separate text fields so you can edit their words without changing the markup.</p>{/if}
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
        <MetadataPanel document={documentPayload} saving={metadataSaving} reextracting={metadataReextracting} on:dirty={(event) => metadataDirty = event.detail} on:save={(event) => saveMetadata(event.detail)} on:reextract={reextractMetadata} />
      {:else}
      <div class="dp-preview-layout" class:has-toc={showToc && activeTab === "html" && !compareWithPdf && !editing} class:compare={compareWithPdf}>
        {#if showToc && activeTab === "html" && !compareWithPdf && !editing}
          <aside class="dp-preview-toc"><h4>Document Outline</h4>
              {#if tocItems.length === 0}<small class="dp-muted">No headings found.</small>{:else}<nav class="dp-toc-nav">{#each tocItems as item (item.key)}<button class="dp-toc-item {item.tag}" type="button" on:click={() => scrollToHeading(item)}>{item.text || "Untitled Heading"}</button>{/each}</nav>{/if}
          </aside>
        {/if}
        {#if compareWithPdf}
          <section class="dp-compare-pdf-pane" aria-label="Original PDF"><header><strong><Icon name="file" size={16} /> Original PDF</strong>{#if downloadUrl}<a class="dp-icon-btn" href={downloadUrl} download={documentPayload?.filename || "document.pdf"} aria-label="Download original PDF" title="Download original PDF"><Icon name="download" size={16} /></a>{/if}</header>
            {#if documentId && pdfUrl}<PdfSourceViewer {documentId} filename={documentPayload?.filename || "document.pdf"} revision={documentPayload?.updated_at || ""} />{:else}<div class="dp-empty-state"><Icon name="file" size={20} /><span>No source PDF available.</span></div>{/if}
          </section>
        {/if}
        <div class="dp-preview-content-pane" class:compare={compareWithPdf}>
          {#if compareWithPdf}<header class="dp-compare-text-header"><strong>{editing ? "Semantic text editor" : activeTab === "md" ? "Extracted Markdown" : "Extracted Text"}</strong>{#if documentId}<button class="dp-icon-btn" type="button" disabled={editing || saving} aria-label="Export extracted document" title={editing || saving ? "Save edits before exporting" : "Export extracted document"} on:click={() => dispatch("export", documentId)}><Icon name="arrow-up-right" size={16} /></button>{/if}</header>{/if}
          {#if !editing}
            <div bind:this={previewPane} class:active={activeTab === "html"} class="dp-preview-pane html">{@html previewHtml}</div>
          {:else}
            <nav class="dp-editor-pages" aria-label="Text correction pages">
              <button class="button" type="button" disabled={editorPage === 0 || saving} on:click={() => editorPage -= 1}>Previous blocks</button>
              <label>Text block <input aria-label="Go to text block" type="number" min="1" max={blocks.length || 1} value={editorPage * editorPageSize + 1} disabled={saving || !blocks.length} on:change={(event) => { const n = Number(event.currentTarget.value); if (Number.isInteger(n) && n >= 1 && n <= blocks.length) editorPage = Math.floor((n - 1) / editorPageSize); }} /></label>
              <span role="status">{blocks.length ? editorPage * editorPageSize + 1 : 0}–{Math.min((editorPage + 1) * editorPageSize, blocks.length)} of {blocks.length}</span>
              <button class="button" type="button" disabled={(editorPage + 1) * editorPageSize >= blocks.length || saving} on:click={() => editorPage += 1}>Next blocks</button>
            </nav>
            <div class:active={activeTab === "html"} class="dp-preview-pane html editing-blocks" aria-describedby="dp-semantic-edit-help"><div class="dp-blocks-list" inert={saving}>
              {#if !blocks.length}<p class="dp-muted">No independently editable text blocks were found. The semantic preview remains read-only.</p>{/if}
              {#each visibleBlocks as block, index (block.id)}<div class="dp-block-item" data-tag={block.tag}><span class="dp-block-tag-badge">{block.tag}</span><SemanticTextEditor html={block.html} label={`Edit ${block.tag} content`} on:change={(event) => updateBlock(editorPage * editorPageSize + index, event.detail)} /></div>{/each}
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
