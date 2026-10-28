<script lang="ts">
  import { EpubCoverCrop, EpubEditionControls, EpubImageFormat, EpubReviewPanel, Icon, prepareDownload, requireEpubEdition, requireEpubPreflight, resetEpubCoverReview, type EpubEdition, type EpubImageMode, type EpubOptions, type EpubPreflight, type EpubQuality } from "@carlina/ui";
  import { createEventDispatcher, onDestroy } from "svelte";
  import { waitForJob, requestJson } from "../lib/api";
  import BaseModal from "./BaseModal.svelte";
  import { canonicalDocumentMetadata } from "../lib/documentMetadata";
  import type { ProcessedDocument } from "../lib/types";

  export let open = false;
  export let documentId: string | null = null;
  export let document: ProcessedDocument | null = null;

  const dispatch = createEventDispatcher<{ close: void; inspect: { documentId: string; anchor: string } }>();
  let format: "md" | "html" | "epub" | "docx" | "pdf" | "debug_zip" = "md";
  let startH2OnNewPage = false;
  let imageMode: EpubImageMode = "jpeg";
  let reviewedImageMode: EpubImageMode = "auto";
  let coverMode = "existing";
  let coverPage: number | undefined = 1;
  let coverCrop: number[] | null = null;
  let epubEdition: EpubEdition | null = null;
  let editionLoadKey: string | null = null;
  let editionLoading = false;
  let editionSequence = 0;
  let coverDocumentId: string | null = null;
  let epubOptions: EpubOptions = {};
  let epubPreflight: EpubPreflight | null = null;
  let epubQuality: EpubQuality | null = null;
  let reviewedCover = "";
  $: metadata = canonicalDocumentMetadata(document);
  $: currentCoverSignature = JSON.stringify([coverMode, coverMode === "pdf_page" ? [coverPage, coverCrop] : null, coverMode === "generated" ? metadata : null]);
  $: if (epubPreflight && currentCoverSignature !== reviewedCover) {
    epubOptions = resetEpubCoverReview(epubOptions, epubPreflight);
    epubPreflight = null;
    epubQuality = null;
  }
  $: if (epubPreflight && imageMode !== reviewedImageMode) {
    epubPreflight = null;
    epubQuality = null;
  }
  $: sourceIsPdf = document?.filename.toLowerCase().endsWith(".pdf") && document.source_available !== false;
  $: if (documentId !== coverDocumentId) {
    coverDocumentId = documentId;
    coverMode = "existing";
    imageMode = "jpeg";
    coverCrop = null;
    epubEdition = null;
    editionLoadKey = null;
    editionSequence += 1;
    editionLoading = false;
    coverPage = metadata.cover_candidate?.page || 1;
    epubOptions = {};
    epubPreflight = null;
    epubQuality = null;
    reviewedCover = "";
  }
  $: if (!open) editionLoadKey = null;
  $: if (open && format === "epub" && documentId && editionLoadKey !== documentId) {
    editionLoadKey = documentId;
    void loadInitialEdition();
  }
  let busy = false;
  let status = "";
  let error = "";
  let prepared: Awaited<ReturnType<typeof prepareDownload>> | null = null;
  const controller = new AbortController();
  onDestroy(() => { controller.abort(); prepared?.dispose(); });

  function selectedCoverSignature(): string {
    return JSON.stringify([coverMode, coverMode === "pdf_page" ? [coverPage, coverCrop] : null, coverMode === "generated" ? metadata : null]);
  }

  function selectedCover() {
    return coverMode === "existing" ? {} : { mode: coverMode, ...(coverMode === "pdf_page" ? { page: coverPage, ...(coverCrop ? { crop: coverCrop } : {}) } : {}) };
  }

  async function loadInitialEdition() {
    const id = documentId;
    try { await loadEdition(); }
    catch (caught) { if (documentId === id) error = caught instanceof Error ? caught.message : "Saved edition choices could not be loaded."; }
  }

  async function loadEdition() {
    if (!documentId) return;
    const id = documentId;
    const sequence = ++editionSequence;
    editionLoading = true;
    try {
    const edition = requireEpubEdition(await requestJson<unknown>(`/addons/doc_processor/api/documents/${encodeURIComponent(id)}/epub/edition`, { signal: controller.signal }));
    if (documentId !== id || sequence !== editionSequence) return;
    epubEdition = edition;
    if (edition.effective_recipe) {
      epubOptions = edition.effective_recipe.epub_options;
      coverMode = edition.effective_recipe.epub_cover.mode || "existing";
      coverPage = edition.effective_recipe.epub_cover.page || metadata.cover_candidate?.page || 1;
      coverCrop = edition.effective_recipe.epub_cover.crop || null;
      imageMode = edition.effective_recipe.epub_image_mode;
      epubPreflight = null; epubQuality = null;
    }
    } finally { if (sequence === editionSequence) editionLoading = false; }
  }

  async function saveEdition() {
    if (!documentId || !epubEdition) return;
    const id = documentId;
    const version = epubEdition.version;
    editionLoading = true;
    try {
    const review = await reviewEpub(epubOptions);
    if (documentId !== id) throw new Error("The selected book changed. Load its edition choices again.");
    if (!review.edition_revision) throw new Error("Review the current source before saving edition choices.");
    const edition = requireEpubEdition(await requestJson<unknown>(`/addons/doc_processor/api/documents/${encodeURIComponent(id)}/epub/edition`, {
      method: "PUT", signal: controller.signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: version, expected_source_revision: review.edition_revision, recipe: { epub_options: epubOptions, epub_cover: selectedCover(), epub_image_mode: imageMode } })
    }));
    if (documentId === id) epubEdition = edition;
    } finally { if (documentId === id) editionLoading = false; }
  }

  async function clearEdition() {
    if (!documentId || !epubEdition) return;
    const id = documentId;
    editionLoading = true;
    try {
    const edition = requireEpubEdition(await requestJson<unknown>(`/addons/doc_processor/api/documents/${encodeURIComponent(id)}/epub/edition`, {
      method: "DELETE", signal: controller.signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_version: epubEdition.version })
    }));
    if (documentId === id) epubEdition = edition;
    } finally { if (documentId === id) editionLoading = false; }
  }

  async function reviewEpub(options: EpubOptions): Promise<EpubPreflight> {
    if (!documentId) throw new Error("Choose a document to review.");
    const reviewedDocumentId = documentId;
    const requestedImageMode = imageMode;
    if (coverMode === "pdf_page" && (!sourceIsPdf || !Number.isInteger(coverPage) || !coverPage || coverPage < 1 || coverPage > 1000000)) throw new Error("Choose a positive physical page number from the source PDF.");
    const cover = selectedCover();
    const coverSignature = selectedCoverSignature();
    if (epubPreflight && coverSignature !== reviewedCover) options = resetEpubCoverReview(options, epubPreflight);
    const result = requireEpubPreflight(await requestJson<unknown>(`/addons/doc_processor/api/documents/${encodeURIComponent(documentId)}/epub/preflight`, {
      method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ epub_options: options, epub_cover: cover, epub_image_mode: requestedImageMode })
    }));
    if (documentId !== reviewedDocumentId) throw new Error("The selected book changed during review. Review the current book again.");
    if (coverSignature !== selectedCoverSignature()) throw new Error("The cover changed during review. Review the selected cover again.");
    if (requestedImageMode !== imageMode) throw new Error("The image format changed during review. Review again before exporting.");
    if (!result.revision) throw new Error("The EPUB review has no source revision. Review again before exporting.");
    epubOptions = result.options;
    reviewedCover = coverSignature;
    reviewedImageMode = requestedImageMode;
    epubPreflight = result;
    epubQuality = null;
    return result;
  }

  async function submit() {
    if (!documentId || busy || editionLoading) {
      return;
    }
    let url = format === "debug_zip"
      ? `/addons/doc_processor/api/documents/${documentId}/export-debug`
      : `/addons/doc_processor/api/documents/${documentId}/export?format=${format}`;
    if (format === "epub") {
      url += `&epub_image_mode=${imageMode}`;
      if (coverMode === "pdf_page" && (!sourceIsPdf || !Number.isInteger(coverPage) || !coverPage || coverPage < 1 || coverPage > 1000000)) {
        error = "Choose a positive physical page number from the source PDF.";
        return;
      }
    }
    if ((format === "docx" || format === "pdf") && startH2OnNewPage) url += "&start_h2_on_new_page=true";
    if (format !== "epub" && format !== "docx" && format !== "pdf") {
      window.open(url, "_blank", "noopener,noreferrer");
      dispatch("close");
      return;
    }
    busy = true;
    prepared?.dispose();
    prepared = null;
    const requestedFormat = format;
    const requestedDocumentId = documentId;
    error = "";
    status = `Queued ${format.toUpperCase()} export...`;
    try {
      const review = requestedFormat === "epub" ? await reviewEpub(epubOptions) : null;
      const payload = await requestJson<{ job_id: string; download_url?: string }>(url, {
        headers: { Accept: "application/json", ...(review ? { "Content-Type": "application/json" } : {}) },
        ...(review ? { method: "POST", body: JSON.stringify({ epub_options: epubOptions, epub_cover: selectedCover(), expected_revision: review.revision, epub_review_hash: review.review_hash }) } : {})
      });
      const completed = await waitForJob(payload.job_id, (jobStatus) => {
        status = jobStatus.message || `Exporting ${format.toUpperCase()}...`;
      });
      const downloadUrl = completed.result?.download_url || payload.download_url;
      epubQuality = (completed.result?.quality as EpubQuality | undefined) || null;
      if (!downloadUrl) {
        throw new Error(`${format.toUpperCase()} export completed without a download URL.`);
      }
      prepared = await prepareDownload(downloadUrl, `Document-${requestedDocumentId}.${requestedFormat}`, { signal: controller.signal });
      status = "Export ready. Select Download prepared document to save it.";
    } catch (err) {
      error = err instanceof Error ? err.message : "Failed to export document.";
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal {open} title="Export Document" size={format === "epub" ? "large" : "small"} on:close={() => dispatch("close")}>
  <form class="dp-export-form" on:submit|preventDefault={submit}>
    <label><input type="radio" bind:group={format} value="md" /> Markdown (.md)</label>
    <label><input type="radio" bind:group={format} value="html" /> Semantic HTML (.html)</label>
    <label><input type="radio" bind:group={format} value="epub" /> eBook (.epub)</label>
    <label><input type="radio" bind:group={format} value="docx" /> Word Document (.docx)</label>
    <label><input type="radio" bind:group={format} value="pdf" /> PDF (.pdf)</label>
    <label><input type="radio" bind:group={format} value="debug_zip" /> Pipeline Debug Archive (.zip)</label>
    {#if format === "epub"}
      <EpubEditionControls edition={epubEdition} busy={busy || editionLoading} onload={loadEdition} onsave={saveEdition} onclear={clearEdition} />
      <fieldset disabled={busy || editionLoading}>
        <legend>Cover</legend>
        <label><input type="radio" bind:group={coverMode} value="existing" /> Keep existing cover</label>
        <label><input type="radio" bind:group={coverMode} value="pdf_page" disabled={!sourceIsPdf} /> Use a PDF page</label>
        <label><input type="radio" bind:group={coverMode} value="generated" /> Generate from title and authors</label>
        {#if coverMode === "pdf_page"}
          <label>Physical PDF page <input type="number" min="1" max="1000000" step="1" bind:value={coverPage} /></label>
          <p class="dp-muted">Count from the first PDF page, including unnumbered pages.</p>
          {#if metadata.cover_candidate}<p class="dp-muted">Suggested page {metadata.cover_candidate.page}: {metadata.cover_candidate.evidence}</p>{/if}
          {#if documentId && coverPage && Number.isInteger(coverPage) && coverPage > 0}
            <EpubCoverCrop bind:crop={coverCrop} {busy} src={`/addons/doc_processor/api/documents/${encodeURIComponent(documentId)}/pdf-pages/${coverPage - 1}/thumbnail?width=400`} />
          {/if}
        {:else if coverMode === "generated"}
          <p class="dp-muted">Uses the saved book details: {metadata.title || document?.filename || "Document"}{metadata.contributors?.authors?.length ? ` — ${metadata.contributors.authors.join(", ")}` : ""}.</p>
        {/if}
      </fieldset>
      <EpubImageFormat bind:mode={imageMode} busy={busy || editionLoading} />
      <EpubReviewPanel bind:options={epubOptions} preflight={epubPreflight} quality={epubQuality} {imageMode} busy={busy || editionLoading} onreview={reviewEpub} oninspect={(anchor) => { if (documentId) dispatch("inspect", { documentId, anchor }); }} />
    {/if}
    {#if format === "docx" || format === "pdf"}
      <fieldset disabled={busy}>
        <legend>Page layout</legend>
        <label><input type="checkbox" bind:checked={startH2OnNewPage} /> Start H2 headings on a new page</label>
        <p class="dp-muted">Useful when H2 marks chapters or letters.</p>
      </fieldset>
    {/if}
    {#if status}<p class="dp-muted">{status}</p>{/if}
    {#if prepared}<a href={prepared.url} download={prepared.filename}>Download prepared document</a>{/if}
    {#if error}<div class="dp-notice error">{error}</div>{/if}
  </form>

  <div slot="footer" class="dp-modal-footer">
    <button class="button ghost" type="button" disabled={busy} on:click={() => dispatch("close")}>Cancel</button>
    <button class="button" type="button" disabled={busy || editionLoading} on:click={submit}>
      {#if busy}<Icon name="refresh" size={15} class="dp-spin" />{:else}<Icon name="download" size={15} />{/if}
      Download
    </button>
  </div>
</BaseModal>
