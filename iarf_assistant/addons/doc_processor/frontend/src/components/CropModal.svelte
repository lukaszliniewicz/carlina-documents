<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher, onDestroy } from "svelte";
  import { loadCropPages, loadCropPreview, saveCrop } from "../lib/api";
  import type { CropBox, CropPage, CropPreviewData } from "../lib/types";
  import { clamp } from "../lib/utils";
  import BaseModal from "./BaseModal.svelte";
  import CropCanvas from "./CropCanvas.svelte";
  import CropPageCanvas from "./CropPageCanvas.svelte";

  export let open = false;
  export let documentId: string | null = null;

  const dispatch = createEventDispatcher<{ close: void; saved: void }>();
  const rowHeight = 224;
  let initialCropState = "";

  let lastDocumentId: string | null = null;
  let pages: CropPage[] = [];
  let loading = false;
  let saving = false;
  let error = "";
  let cropMode: "single" | "left_right" = "left_right";
  let firstPageSide: "left" | "right" = "right";
  let excludedPages = new Set<number>();
  let suggestedExcludedPages = new Set<number>();
  let singleCrop: CropBox | null = null;
  let leftCrop: CropBox | null = null;
  let rightCrop: CropBox | null = null;
  let previews: Partial<Record<"single" | "left" | "right", CropPreviewData | null>> = {};
  let suggestedPreviewBoxes: Partial<Record<"single" | "left" | "right", CropBox | null>> = {};
  let previewLoading = false;
  let previewAttempted = false;
  let previewError = "";
  let previewRequestId = 0;
  let previewTimer: ReturnType<typeof setTimeout> | null = null;
  let previewAbortController: AbortController | null = null;
  let loadGeneration = 0;
  let loadAbortController: AbortController | null = null;
  let pageListEl: HTMLDivElement;
  let scrollTop = 0;
  let viewportHeight = 600;
  let selectedPageIndex: number | null = null;
  let previewMode: "stack" | "page" = "stack";

  $: if (open && documentId && documentId !== lastDocumentId) {
    lastDocumentId = documentId;
    initialize(documentId);
  }

  $: if (!open) {
    loadAbortController?.abort();
    lastDocumentId = null;
  }

  $: inRangePages = pages.filter((page) => page.in_page_range !== false);
  $: includedPages = inRangePages.filter((page) => !excludedPages.has(page.page_index));
  $: excludedCount = inRangePages.length - includedPages.length;
  $: pageSideMap = buildPageSideMap(inRangePages, firstPageSide);
  $: previewPageCandidates = includedPages.length ? includedPages : inRangePages;
  $: if (open && pages.length && selectedPageIndex === null) {
    selectedPageIndex = (previewPageCandidates[0] || inRangePages[0] || pages[0])?.page_index ?? null;
  }
  $: selectedPage = selectedPageIndex === null ? null : pages.find((page) => page.page_index === selectedPageIndex) || null;
  $: selectedPageSide = cropMode === "left_right" && selectedPage
    ? pageSideMap.get(selectedPage.page_index) || selectedPage.side || firstPageSide
    : null;
  $: selectedPageCrop = selectedPage
    ? cropMode === "single"
      ? singleCrop
      : selectedPageSide === "left" ? leftCrop : rightCrop
    : null;
  $: selectedPageSuggestedCrop = selectedPage
    ? cropMode === "single"
      ? suggestedPreviewBoxes.single || null
      : selectedPageSide === "left" ? suggestedPreviewBoxes.left || null : suggestedPreviewBoxes.right || null
    : null;
  $: selectedPageImageSrc = selectedPage ? pagePreviewUrl(selectedPage) : "";
  $: selectedPageMeta = selectedPage
    ? buildSelectedPageMeta(selectedPage, selectedPageSide, cropMode, selectedPageCrop, selectedPageSuggestedCrop, excludedPages)
    : "";
  $: selectedPageCanvasKey = [
    cropMode,
    selectedPageIndex ?? "none",
    selectedPageSide || "single"
  ].join(":");
  $: startIndex = Math.max(0, Math.floor(scrollTop / rowHeight) - 3);
  $: visibleCount = Math.ceil(viewportHeight / rowHeight) + 6;
  $: visiblePages = pages.slice(startIndex, startIndex + visibleCount);
  $: topPad = startIndex * rowHeight;
  $: bottomPad = Math.max(0, (pages.length - startIndex - visiblePages.length) * rowHeight);
  $: skippedByRange = Math.max(0, pages.length - inRangePages.length);
  $: suggestedPreviewBoxes = {
    single: normaliseCropBox(previews.single?.suggested_crop_box),
    left: normaliseCropBox(previews.left?.suggested_crop_box),
    right: normaliseCropBox(previews.right?.suggested_crop_box)
  };
  $: previewImageSources = {
    single: previews.single?.image_url || previews.single?.image_data || "",
    left: previews.left?.image_url || previews.left?.image_data || "",
    right: previews.right?.image_url || previews.right?.image_data || ""
  };
  $: summary = pages.length
    ? `${includedPages.length}/${inRangePages.length} included${skippedByRange ? ` | ${skippedByRange} skipped by range` : ""}${excludedCount ? ` | ${excludedCount} excluded` : ""}`
    : "Loading pages...";
  $: cropState = JSON.stringify([cropMode, firstPageSide, [...excludedPages].sort((a,b) => a-b), singleCrop, leftCrop, rightCrop]);
  $: dirty = Boolean(initialCropState) && cropState !== initialCropState;

  onDestroy(() => {
    if (previewTimer) {
      clearTimeout(previewTimer);
    }
    if (previewAbortController) {
      previewAbortController.abort();
    }
    if (loadAbortController) {
      loadAbortController.abort();
    }
  });

  function close() {
    dispatch("close");
  }

  async function initialize(id: string) {
    const generation = ++loadGeneration;
    loadAbortController?.abort();
    const controller = new AbortController();
    loadAbortController = controller;
    pages = [];
    previews = {};
    excludedPages = new Set();
    suggestedExcludedPages = new Set();
    cropMode = "left_right";
    firstPageSide = "right";
    singleCrop = null;
    leftCrop = null;
    rightCrop = null;
    selectedPageIndex = null;
    previewMode = "stack";
    error = "";
    initialCropState = "";
    scrollTop = 0;
    if (pageListEl) pageListEl.scrollTop = 0;
    previewError = "";
    previewAttempted = false;
    previewLoading = true;
    loading = true;
    try {
      const payload = await loadCropPages(id, controller.signal);
      if (controller.signal.aborted || generation !== loadGeneration || id !== documentId) return;
      pages = payload.pages || [];
      applyCropSettings(payload.crop_settings || {});
      initialCropState = JSON.stringify([cropMode, firstPageSide, [...excludedPages].sort((a,b) => a-b), singleCrop, leftCrop, rightCrop]);
      loading = false;
      void refreshPreview();
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError") && generation === loadGeneration) {
        error = err instanceof Error ? err.message : "Failed to load crop review.";
        loading = false;
        previewLoading = false;
      }
    }
  }

  function normaliseCropBox(box: unknown): CropBox | null {
    if (!Array.isArray(box) || box.length !== 4) {
      return null;
    }
    const values = box.map((value) => Number(value));
    if (values.some((value) => !Number.isFinite(value))) {
      return null;
    }
    const [x0, y0, x1, y1] = values;
    const xMin = clamp(Math.min(x0, x1), 0, 1);
    const xMax = clamp(Math.max(x0, x1), 0, 1);
    const yMin = clamp(Math.min(y0, y1), 0, 1);
    const yMax = clamp(Math.max(y0, y1), 0, 1);
    return xMax - xMin < 0.001 || yMax - yMin < 0.001 ? null : [xMin, yMin, xMax, yMax];
  }

  function clone(box: CropBox | null): CropBox | null {
    return box ? [...box] as CropBox : null;
  }

  function buildUnion(boxes: Array<CropBox | null>): CropBox | null {
    const valid = boxes.filter(Boolean) as CropBox[];
    if (!valid.length) {
      return null;
    }
    return [
      Math.min(...valid.map((box) => box[0])),
      Math.min(...valid.map((box) => box[1])),
      Math.max(...valid.map((box) => box[2])),
      Math.max(...valid.map((box) => box[3]))
    ];
  }

  function applyCropSettings(settings: Record<string, unknown>) {
    const cropBoxes = (settings.crop_boxes && typeof settings.crop_boxes === "object" ? settings.crop_boxes : {}) as Record<string, unknown>;
    singleCrop = normaliseCropBox(settings.crop_box);
    leftCrop = normaliseCropBox(cropBoxes.left);
    rightCrop = normaliseCropBox(cropBoxes.right);
    firstPageSide = cropBoxes.first_page_side === "left" ? "left" : "right";
    excludedPages = new Set(Array.isArray(settings.excluded_pages) ? settings.excluded_pages.map(Number).filter(Number.isInteger) : []);
    suggestedExcludedPages = new Set(
      Array.isArray(settings.metadata_suggested_excluded_pages)
        ? settings.metadata_suggested_excluded_pages.map(Number).filter(Number.isInteger)
        : []
    );
    const hasSplit = Boolean(leftCrop || rightCrop);
    cropMode = cropBoxes.mode === "single" && !hasSplit ? "single" : "left_right";
    if (cropMode === "left_right") {
      copySingleToSplit();
    } else if (!singleCrop) {
      singleCrop = buildUnion([leftCrop, rightCrop]);
    }
  }

  function copySingleToSplit() {
    if (!singleCrop) {
      return;
    }
    leftCrop = leftCrop || clone(singleCrop);
    rightCrop = rightCrop || clone(singleCrop);
  }

  function buildPageSideMap(rangePages: CropPage[], firstSide: "left" | "right") {
    const sideMap = new Map<number, "left" | "right">();
    let currentSide: "left" | "right" = firstSide;
    for (const page of rangePages.slice().sort((a, b) => a.page_index - b.page_index)) {
      sideMap.set(page.page_index, currentSide);
      currentSide = currentSide === "right" ? "left" : "right";
    }
    return sideMap;
  }

  function selectPage(page: CropPage) {
    if (page.in_page_range === false) {
      return;
    }
    selectedPageIndex = page.page_index;
    previewMode = "page";
  }

  function showStackPreview() {
    previewMode = "stack";
  }

  function selectAdjacentPage(delta: number) {
    const candidates = previewPageCandidates.length ? previewPageCandidates : inRangePages;
    if (!candidates.length) {
      return;
    }
    const sorted = candidates.slice().sort((a, b) => a.page_index - b.page_index);
    const currentIndex = selectedPageIndex === null
      ? -1
      : sorted.findIndex((page) => page.page_index === selectedPageIndex);
    const nextIndex = currentIndex === -1
      ? (delta > 0 ? 0 : sorted.length - 1)
      : (currentIndex + delta + sorted.length) % sorted.length;
    selectedPageIndex = sorted[nextIndex].page_index;
    revealPage(selectedPageIndex);
  }

  function revealPage(index: number) {
    const position = pages.findIndex(page => page.page_index === index);
    scrollTop = Math.max(0, position * rowHeight);
    if (pageListEl) pageListEl.scrollTop = scrollTop;
  }

  function jumpToPage(raw: string) {
    const page = pages.find(item => item.page_number === Number(raw));
    if (page && page.in_page_range !== false) { selectPage(page); revealPage(page.page_index); }
  }

  function pagePreviewUrl(page: CropPage): string {
    const [path, query = ""] = page.thumbnail_url.split("?", 2);
    const params = new URLSearchParams(query);
    params.set("width", "1200");
    return `${path}?${params.toString()}`;
  }

  function cropDimensions(box: CropBox): [number, number] {
    return [Math.max(0.001, box[2] - box[0]), Math.max(0.001, box[3] - box[1])];
  }

  function boxWithSizeAtOrigin(originSource: CropBox, sizeSource: CropBox): CropBox {
    const [width, height] = cropDimensions(sizeSource);
    const x0 = clamp(originSource[0], 0, Math.max(0, 1 - width));
    const y0 = clamp(originSource[1], 0, Math.max(0, 1 - height));
    return [x0, y0, x0 + width, y0 + height];
  }

  function fallbackCropForSide(side: "left" | "right", sizeSource: CropBox): CropBox {
    const sideCrop = side === "left" ? leftCrop : rightCrop;
    const sideSuggested = side === "left" ? suggestedPreviewBoxes.left : suggestedPreviewBoxes.right;
    const oppositeSuggested = side === "left" ? suggestedPreviewBoxes.right : suggestedPreviewBoxes.left;
    const originSource = sideCrop || sideSuggested || singleCrop || oppositeSuggested || sizeSource;
    return boxWithSizeAtOrigin(originSource, sizeSource);
  }

  function synchronizeSplitCropSize(left: CropBox | null, right: CropBox | null, sizeSource: CropBox) {
    leftCrop = left ? boxWithSizeAtOrigin(left, sizeSource) : fallbackCropForSide("left", sizeSource);
    rightCrop = right ? boxWithSizeAtOrigin(right, sizeSource) : fallbackCropForSide("right", sizeSource);
  }

  function updateStackCrop(viewKey: "single" | "left" | "right", box: CropBox) {
    const nextBox = [...box] as CropBox;
    if (viewKey === "single" || cropMode === "single") {
      singleCrop = nextBox;
      return;
    }
    const nextLeft = viewKey === "left" ? nextBox : clone(leftCrop);
    const nextRight = viewKey === "right" ? nextBox : clone(rightCrop);
    synchronizeSplitCropSize(nextLeft, nextRight, nextBox);
  }

  function updateSelectedPageCrop(detail: { box: CropBox; action: "move" | "resize" }) {
    const nextBox = [...detail.box] as CropBox;
    if (cropMode === "single") {
      singleCrop = nextBox;
      return;
    }
    if (selectedPageSide !== "left" && selectedPageSide !== "right") {
      return;
    }

    let nextLeft = clone(leftCrop);
    let nextRight = clone(rightCrop);
    if (selectedPageSide === "left") {
      nextLeft = nextBox;
    } else {
      nextRight = nextBox;
    }
    synchronizeSplitCropSize(nextLeft, nextRight, nextBox);
  }

  function buildSelectedPageMeta(
    page: CropPage,
    side: "left" | "right" | null,
    mode: "single" | "left_right",
    crop: CropBox | null,
    suggestedCrop: CropBox | null,
    excluded: Set<number>
  ): string {
    const parts = [`Page ${page.page_number}`];
    if (page.in_page_range === false) {
      parts.push("outside range");
    } else if (excluded.has(page.page_index)) {
      parts.push("excluded");
    }
    if (mode === "left_right") {
      parts.push(side === "left" ? "left pages" : "right pages");
    }
    if (!crop && !suggestedCrop) {
      parts.push("no crop selected");
    }
    return parts.join(" | ");
  }

  function changeMode(nextMode: "single" | "left_right") {
    if (nextMode === cropMode) {
      return;
    }
    if (nextMode === "left_right") {
      copySingleToSplit();
    } else if (!singleCrop) {
      singleCrop = buildUnion([leftCrop, rightCrop]);
    }
    cropMode = nextMode;
    queuePreview();
  }

  function togglePage(page: CropPage) {
    if (page.in_page_range === false) {
      return;
    }
    const next = new Set(excludedPages);
    if (next.has(page.page_index)) {
      next.delete(page.page_index);
    } else {
      if (includedPages.length <= 1) {
        error = "At least one page must remain included.";
        return;
      }
      next.add(page.page_index);
    }
    excludedPages = next;
    queuePreview();
  }

  function queuePreview() {
    if (previewTimer) {
      clearTimeout(previewTimer);
    }
    previewLoading = true;
    previewError = "";
    previewTimer = setTimeout(() => {
      previewTimer = null;
      refreshPreview();
    }, 260);
  }

  async function refreshPreview() {
    if (!documentId) {
      return;
    }
    const requestId = previewRequestId + 1;
    previewRequestId = requestId;
    if (previewAbortController) {
      previewAbortController.abort();
    }
    const controller = new AbortController();
    previewAbortController = controller;
    const timeoutId = window.setTimeout(() => controller.abort(), 30_000);
    previewLoading = true;
    previewAttempted = true;
    previewError = "";
    error = "";
    try {
      const payload = await loadCropPreview(documentId, cropMode, firstPageSide, Array.from(excludedPages), controller.signal);
      if (requestId === previewRequestId) {
        previews = payload.previews || {};
      }
    } catch (err) {
      if (requestId === previewRequestId) {
        previewError = err instanceof DOMException && err.name === "AbortError"
          ? "Timed out generating crop preview."
          : err instanceof Error ? err.message : "Failed to generate crop preview.";
        error = previewError;
        previews = {};
      }
    } finally {
      window.clearTimeout(timeoutId);
      if (requestId === previewRequestId) {
        previewLoading = false;
        previewAbortController = null;
      }
    }
  }

  function previewMeta(viewKey: "single" | "left" | "right"): string {
    const preview = previews[viewKey];
    const count = Number(preview?.used_page_count || 0);
    if (!count) {
      return previewLoading ? "Generating..." : "";
    }
    const excluded = Number(preview?.excluded_page_count || 0);
    const excludedText = excluded ? `, ${excluded} excluded` : "";
    const pagesUsed = preview?.used_pages || [];
    if (pagesUsed.length && count <= pagesUsed.length) {
      return `Pages ${pagesUsed.slice(0, 6).join(", ")}${pagesUsed.length > 6 ? "..." : ""}${excludedText}`;
    }
    const total = Number(preview?.used_page_total || count);
    const sampleText = total > count ? `${count}/${total} sampled pages` : `${count} ${count === 1 ? "page" : "pages"}`;
    return `${sampleText}${excludedText}`;
  }

  function previewEmptyMessage(viewKey: "single" | "left" | "right"): string {
    if (loading || previewLoading) {
      return "Generating preview...";
    }
    if (previewError) {
      return `Preview request failed: ${previewError}`;
    }
    const reason = previews[viewKey]?.empty_reason;
    if (reason === "no_pages_for_stack") {
      return "No pages are assigned to this stack.";
    }
    if (reason === "all_pages_excluded") {
      return "All pages in this stack are excluded.";
    }
    if (reason === "stack_unavailable") {
      return "Preview unavailable for this stack. Included pages are still present.";
    }
    if (previews[viewKey]) {
      return "Preview unavailable for this stack.";
    }
    return previewAttempted ? "Preview response did not include this stack." : "Preparing preview...";
  }

  function hasCropSelection() {
    return cropMode === "left_right" ? Boolean(leftCrop || rightCrop) : Boolean(singleCrop);
  }

  function buildSavePayload(clearCropBoxes = false) {
    const single = clearCropBoxes ? null : clone(singleCrop);
    let left = clearCropBoxes ? null : clone(leftCrop);
    let right = clearCropBoxes ? null : clone(rightCrop);
    if (!clearCropBoxes && cropMode === "left_right") {
      if (!left && right) left = clone(right);
      if (!right && left) right = clone(left);
    }
    const fallbackSingle = clearCropBoxes ? null : single || buildUnion([left, right]);
    return {
      crop_box: cropMode === "single" ? single : fallbackSingle,
      crop_boxes: {
        mode: cropMode,
        left: cropMode === "left_right" ? left : null,
        right: cropMode === "left_right" ? right : null,
        first_page_side: firstPageSide
      },
      excluded_pages: Array.from(excludedPages).sort((a, b) => a - b)
    };
  }

  async function submit(clearCropBoxes = false) {
    if (!documentId) {
      return;
    }
    if (!clearCropBoxes && !hasCropSelection() && excludedCount === 0) {
      error = "Draw a crop box first, or exclude at least one page before saving without a crop box.";
      return;
    }
    saving = true;
    error = "";
    try {
      await saveCrop(documentId, buildSavePayload(clearCropBoxes));
      dispatch("saved");
    } catch (err) {
      error = err instanceof Error ? err.message : "Failed to save crop settings.";
    } finally {
      saving = false;
    }
  }
</script>

<BaseModal {open} {dirty} busy={saving} title="Set Document Margins" size="full" on:close={close}>
  <div class="dp-crop-modal">
    <div class="dp-crop-toolbar">
      <label>
        <span>Crop Mode</span>
        <select value={cropMode} on:change={(event) => changeMode(event.currentTarget.value === "left_right" ? "left_right" : "single")}>
          <option value="single">Single Stack</option>
          <option value="left_right">Left / Right Stacks</option>
        </select>
      </label>
      <label>
        <span>First Included Page</span>
        <select
          value={firstPageSide}
          disabled={cropMode !== "left_right"}
          on:change={(event) => {
            firstPageSide = event.currentTarget.value === "left" ? "left" : "right";
            queuePreview();
          }}
        >
          <option value="right">Right Page</option>
          <option value="left">Left Page</option>
        </select>
      </label>
      <label><span>Preview page</span><input aria-label="Preview PDF page" type="number" min="1" max={pages.length || 1} value={(selectedPageIndex ?? 0) + 1} on:change={(event) => jumpToPage(event.currentTarget.value)} on:keydown={(event) => { if (event.key === "Enter") { event.preventDefault(); jumpToPage(event.currentTarget.value); } }} /></label>
      <div class="dp-crop-summary">{summary}</div>
    </div>

    {#if error}
      <div class="dp-notice error">{error}</div>
    {/if}

    <div class="dp-crop-body">
      <aside class="dp-crop-page-panel">
        <header>
          <strong>Pages</strong>
          <small>Exclude outliers</small>
        </header>
        <div
          class="dp-crop-page-list"
          style={`--dp-crop-row-height:${rowHeight}px`}
          bind:this={pageListEl}
          bind:clientHeight={viewportHeight}
          on:scroll={(event) => {
            scrollTop = event.currentTarget.scrollTop;
            viewportHeight = event.currentTarget.clientHeight || viewportHeight;
          }}
        >
          {#if loading || !pages.length}
            <p class="dp-muted">{loading ? "Loading page previews..." : "No pages available."}</p>
          {:else}
            <div style={`height:${topPad}px`}></div>
            {#each visiblePages as page (page.page_index)}
              {@const isInRange = page.in_page_range !== false}
              {@const isExcluded = isInRange && excludedPages.has(page.page_index)}
              {@const isSuggested = isInRange && (suggestedExcludedPages.has(page.page_index) || page.metadata_suggested_exclusion)}
              {@const side = cropMode === "left_right" && isInRange && !isExcluded ? pageSideMap.get(page.page_index) : null}
              <article
                class:is-excluded={isExcluded}
                class:is-out-of-range={!isInRange}
                class:is-selected={selectedPageIndex === page.page_index}
                class="dp-crop-page-card"
              >
                <div>
                  <strong>Page {page.page_number}</strong>
                  <div class="dp-crop-page-actions">
                    <button class="button compact" type="button" disabled={!isInRange} title="Preview page" on:click={() => selectPage(page)}>
                      <Icon name="eye" size={16} />
                    </button>
                    <button class="button compact" type="button" disabled={!isInRange} on:click={() => togglePage(page)}>
                      {isExcluded ? "Restore" : "Exclude"}
                    </button>
                  </div>
                </div>
                <div class="dp-crop-flags">
                  {#if !isInRange}<span>Page Range</span>{/if}
                  {#if isExcluded}<span class="danger">Excluded</span>{/if}
                  {#if isSuggested}<span class="warning">Pre-Pass</span>{/if}
                  {#if side}<span>{side === "left" ? "Left" : "Right"}</span>{/if}
                </div>
                <button class="dp-crop-page-thumb" type="button" disabled={!isInRange} title="Preview page" on:click={() => selectPage(page)}>
                  <img loading="lazy" src={page.thumbnail_url} alt={`Preview of page ${page.page_number}`} />
                </button>
              </article>
            {/each}
            <div style={`height:${bottomPad}px`}></div>
          {/if}
        </div>
      </aside>

      <section class:split={cropMode === "left_right" && previewMode === "stack"} class:page-mode={previewMode === "page"} class="dp-crop-preview-grid">
        {#if previewMode === "page"}
          {#key selectedPageCanvasKey}
            <CropPageCanvas
              title="Page Preview"
              imageSrc={selectedPageImageSrc}
              cropBox={selectedPageCrop}
              suggestedBox={selectedPageSuggestedCrop}
              meta={selectedPageMeta}
              emptyMessage={loading ? "Loading page previews..." : "No page selected."}
              on:cropBoxChange={(event) => updateSelectedPageCrop(event.detail)}
            >
              <svelte:fragment slot="actions">
                <button class="dp-icon-btn" type="button" title="Previous included page" disabled={!previewPageCandidates.length} on:click={() => selectAdjacentPage(-1)}>
                  <Icon name="chevron-left" size={16} />
                </button>
                <button class="dp-icon-btn" type="button" title="Next included page" disabled={!previewPageCandidates.length} on:click={() => selectAdjacentPage(1)}>
                  <Icon name="chevron-right" size={16} />
                </button>
                <button class="button compact" type="button" on:click={showStackPreview}>
                  <Icon name="workspaces" size={16} />
                  Stacks
                </button>
              </svelte:fragment>
            </CropPageCanvas>
          {/key}
        {:else}
          {#if cropMode === "left_right"}
            <CropCanvas cropBox={leftCrop} title="Left Pages" imageSrc={previewImageSources.left} meta={previewMeta("left")} loading={previewLoading} emptyMessage={previewEmptyMessage("left")} suggestedBox={suggestedPreviewBoxes.left || null} on:cropBoxChange={(event) => updateStackCrop("left", event.detail)} />
            <CropCanvas cropBox={rightCrop} title="Right Pages" imageSrc={previewImageSources.right} meta={previewMeta("right")} loading={previewLoading} emptyMessage={previewEmptyMessage("right")} suggestedBox={suggestedPreviewBoxes.right || null} on:cropBoxChange={(event) => updateStackCrop("right", event.detail)} />
          {:else}
            <CropCanvas cropBox={singleCrop} title="Combined Stack" imageSrc={previewImageSources.single} meta={previewMeta("single")} loading={previewLoading} emptyMessage={previewEmptyMessage("single")} suggestedBox={suggestedPreviewBoxes.single || null} on:cropBoxChange={(event) => updateStackCrop("single", event.detail)} />
          {/if}
        {/if}
      </section>
    </div>
  </div>

  <div slot="footer" class="dp-modal-footer">
    <div>
      <button class="button" type="button" on:click={() => {
        singleCrop = null;
        leftCrop = null;
        rightCrop = null;
      }}>Reset</button>
      <button class="button danger" type="button" disabled={saving} on:click={() => submit(true)}>
        Skip Cropping
      </button>
    </div>
    <button class="button success" type="button" disabled={saving} on:click={() => submit(false)}>
      {#if saving}<Icon name="refresh" size={16} class="dp-spin" />{/if}
      Save and Begin
    </button>
  </div>
</BaseModal>
