<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { onDestroy } from "svelte";
  import { getPdfPageImageUrl, getSourceFileUrl, loadCropPages } from "../lib/api";

  export let documentId: string;
  export let filename = "Source PDF";
  export let revision = "";
  let loadedId = "";
  let controller: AbortController | null = null;
  let pageCount = 0;
  let pageNumber = 1;
  let zoom = 100;
  let loading = false;
  let imageLoading = false;
  let imageFailed = false;
  let error = "";
  let lastImage = "";
  let retry = 0;

  $: if (documentId && loadedId !== documentId) {
    loadedId = documentId;
    void load(documentId);
  }
  $: imageUrl = pageCount ? `${getPdfPageImageUrl(documentId, pageNumber - 1, revision)}&retry=${retry}` : "";
  $: if (imageUrl !== lastImage) {
    lastImage = imageUrl;
    imageLoading = Boolean(imageUrl);
    imageFailed = false;
  }
  onDestroy(() => controller?.abort());

  async function load(id: string) {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    pageCount = 0;
    pageNumber = 1;
    loading = true;
    error = "";
    try {
      const payload = await loadCropPages(id, request.signal);
      if (request.signal.aborted || id !== documentId) return;
      pageCount = payload.total_pages || payload.pages?.length || 0;
      if (!pageCount) error = "No pages were found in this PDF.";
    } catch (caught) {
      if (!request.signal.aborted && id === documentId) error = caught instanceof Error ? caught.message : "Unable to load the source PDF.";
    } finally {
      if (!request.signal.aborted && id === documentId) loading = false;
    }
  }

  function choosePage(raw: string) {
    const number = Number(raw);
    if (Number.isInteger(number) && number >= 1 && number <= pageCount) pageNumber = number;
  }
</script>

<section class="pdf-source-viewer" aria-label="Source PDF pages">
  <nav aria-label="Source PDF navigation">
    <button type="button" class="button compact" aria-label="Previous PDF page" disabled={pageNumber <= 1 || loading} on:click={() => pageNumber -= 1}><Icon name="chevron-left" size={16} /></button>
    <label>Page <input aria-label="Source PDF page" type="number" min="1" max={pageCount || 1} value={pageNumber} disabled={!pageCount} on:change={(event) => choosePage(event.currentTarget.value)} /></label>
    <span>of {pageCount || "…"}</span>
    <button type="button" class="button compact" aria-label="Next PDF page" disabled={!pageCount || pageNumber >= pageCount || loading} on:click={() => pageNumber += 1}><Icon name="chevron-right" size={16} /></button>
    <label>Zoom <select bind:value={zoom}><option value={100}>Fit width</option><option value={125}>125%</option><option value={150}>150%</option><option value={200}>200%</option></select></label>
    <a class="button compact" href={getSourceFileUrl(documentId, "attachment")} download={filename}><Icon name="download" size={16} /> Original PDF</a>
  </nav>
  {#if loading}<p role="status">Loading source pages…</p>{/if}
  {#if error}<p role="alert">{error} <button class="button compact" type="button" on:click={() => load(documentId)}>Retry</button></p>{/if}
  {#if imageFailed}<p role="alert">The page preview could not be loaded. <button type="button" class="button compact" on:click={() => retry += 1}>Retry page</button></p>{:else if imageLoading}<p class="page-status" role="status">Loading page {pageNumber}…</p>{/if}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (The labelled overflow region must accept keyboard focus for scrolling.) -->
  <div class="source-page-scroll" role="region" tabindex="0" aria-label="Scrollable source page">
    {#if imageUrl}
      {#key imageUrl}<img src={imageUrl} alt={`Page ${pageNumber} of ${filename}`} style={`width:${zoom}%`} on:load={() => imageLoading = false} on:error={() => { imageLoading = false; imageFailed = true; }} />{/key}
    {/if}
  </div>
</section>

<style>
  .pdf-source-viewer { display:flex; flex:1; flex-direction:column; min-height:0; height:100%; position:relative; }
  nav { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; padding:.5rem; border-bottom:1px solid var(--color-rule,#ccc); }
  label { display:flex; align-items:center; gap:.3rem; font-size:.8rem; }
  input { width:4.5rem; }
  input,select { font:inherit; padding:.3rem; color:inherit; background:var(--color-surface,#fff); border:1px solid var(--color-rule,#ccc); }
  .source-page-scroll { flex:1; min-height:0; overflow:auto; background:#ddd9d1; padding:.5rem; }
  img { display:block; max-width:none; height:auto; margin:0 auto; }
  p { margin:.5rem; }
  .page-status { position:absolute; top:4rem; align-self:center; padding:.3rem .6rem; background:var(--color-surface,#fff); }
</style>
