<script lang="ts">
  import { Icon, confirmDiscardChanges } from "@carlina/ui";
  import { createEventDispatcher, onDestroy } from "svelte";
  import { loadCompletedImageCrops, loadImageReviewPages, saveCompletedImageCrops, saveImageReviewPages } from "../lib/api";
  import type { ImageReviewItem, ImageReviewPage } from "../lib/types";
  import { clamp } from "../lib/utils";
  import BaseModal from "./BaseModal.svelte";

  export let open = false;
  export let documentId: string | null = null;
  export let completed = false;

  const dispatch = createEventDispatcher<{ close: void; saved: void }>();

  let lastDocumentId: string | null = null;
  let pages: ImageReviewPage[] = [];
  let originalPages: ImageReviewPage[] = [];
  let expectedUpdatedAt = "";
  let pageCount = 0;
  let sourcePageInvalid = false;
  let pageIndex = 0;
  let imageIndex = 0;
  let loading = false;
  let saving = false;
  let error = "";
  let overlay: HTMLDivElement;
  let loadGeneration = 0;
  let loadAbortController: AbortController | null = null;
  let dirtyPages = new Set<number>();
  let dragFrame: number | null = null;
  let pendingPoint: { x: number; y: number } | null = null;
  let drag:
    | {
        mode: "move" | "resize";
        itemIndex: number;
        startX: number;
        startY: number;
        initial: [number, number, number, number];
        rect: DOMRect;
        pointerId: number;
        target: HTMLElement;
      }
    | null = null;

  $: if (open && documentId && documentId !== lastDocumentId) {
    lastDocumentId = documentId;
    initialize(documentId);
  }

  $: if (!open) {
    loadAbortController?.abort();
    if (lastDocumentId) loadGeneration += 1;
    lastDocumentId = null;
    saving = false;
  }

  $: currentPage = pages[pageIndex] || null;
  $: visibleImages = currentPage ? currentPage.images.filter((image) => image.keep !== false) : [];
  $: selectedImage = currentPage?.images[imageIndex] || null;
  $: validBoxes = pages.every((page) => page.images.every((image) => image.keep === false || (
    image.bbox.every((value) => Number.isFinite(value) && value >= 0 && value <= 1000) &&
    image.bbox[0] < image.bbox[2] && image.bbox[1] < image.bbox[3]
  )));

  onDestroy(() => { loadAbortController?.abort(); if (dragFrame !== null) cancelAnimationFrame(dragFrame); });

  async function initialize(id: string) {
    const generation = ++loadGeneration;
    loadAbortController?.abort();
    const controller = new AbortController();
    loadAbortController = controller;
    loading = true;
    error = "";
    pages = [];
    originalPages = [];
    dirtyPages = new Set();
    expectedUpdatedAt = "";
    pageCount = 0;
    sourcePageInvalid = false;
    drag = null;
    pageIndex = 0;
    imageIndex = 0;
    try {
      const result = completed ? await loadCompletedImageCrops(id, controller.signal) : null;
      const nextPages = result ? result.pages : await loadImageReviewPages(id, controller.signal);
      if (controller.signal.aborted || generation !== loadGeneration || id !== documentId) return;
      pages = nextPages;
      originalPages = structuredClone(nextPages);
      expectedUpdatedAt = result?.expected_updated_at || "";
      pageCount = result?.page_count || 0;
      pageIndex = 0;
      imageIndex = pages[0]?.images.findIndex((image) => image.keep !== false) ?? 0;
      if (imageIndex < 0) imageIndex = 0;
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError") && generation === loadGeneration) {
        error = err instanceof Error ? err.message : "Failed to load image review.";
      }
    } finally {
      if (generation === loadGeneration) loading = false;
    }
  }

  function selectPage(index: number) {
    finishDrag();
    pageIndex = index;
    imageIndex = pages[index]?.images.findIndex((image) => image.keep !== false) ?? 0;
    if (imageIndex < 0) imageIndex = 0;
  }

  function replacePage(index: number, page: ImageReviewPage) {
    const nextPages = [...pages];
    nextPages[index] = page;
    pages = nextPages;
    const changed = new Set(dirtyPages);
    if (JSON.stringify(page) === JSON.stringify(originalPages[index])) changed.delete(index);
    else changed.add(index);
    dirtyPages = changed;
  }

  function updateSelected(patch: Partial<ImageReviewItem>) {
    if (!currentPage || !selectedImage) {
      return;
    }
    const page = { ...currentPage, images: [...currentPage.images] };
    page.images[imageIndex] = { ...selectedImage, ...patch };
    replacePage(pageIndex, page);
  }

  function selectSourcePage(raw: string) {
    if (!currentPage || !documentId || currentPage.source_kind !== "saved_image") return;
    sourcePageInvalid = false;
    if (!raw.trim()) {
      replacePage(pageIndex, structuredClone(originalPages[pageIndex]));
      error = "";
      return;
    }
    const sourcePageIndex = Number(raw) - 1;
    if (!Number.isInteger(sourcePageIndex) || sourcePageIndex < 0 || sourcePageIndex >= pageCount) {
      sourcePageInvalid = true;
      error = `Choose a PDF page from 1 to ${pageCount}.`;
      return;
    }
    error = "";
    replacePage(pageIndex, {
      ...currentPage,
      source_page_index: sourcePageIndex,
      image_url: `/addons/doc_processor/api/documents/${documentId}/pdf-pages/${sourcePageIndex}/thumbnail?width=1800`,
      images: currentPage.images.map((image) => ({ ...image, bbox: [0, 0, 1000, 1000] }))
    });
  }

  function addBox() {
    if (!currentPage) {
      return;
    }
    const page = { ...currentPage, images: [...currentPage.images] };
    const nextIndex = page.images.length;
    page.images.push({
      img_idx: nextIndex,
      bbox: [100, 100, 400, 400],
      caption: "",
      keep: true
    });
    replacePage(pageIndex, page);
    imageIndex = nextIndex;
  }

  function removeBox() {
    updateSelected({ keep: false });
    imageIndex = currentPage?.images.findIndex((image) => image.keep !== false) ?? 0;
  }

  function boxStyle(item: ImageReviewItem): string {
    const [yMin, xMin, yMax, xMax] = item.bbox;
    return [
      `top:${yMin / 10}%`,
      `left:${xMin / 10}%`,
      `width:${(xMax - xMin) / 10}%`,
      `height:${(yMax - yMin) / 10}%`
    ].join(";");
  }

  function beginDrag(event: PointerEvent, itemIndex: number, mode: "move" | "resize") {
    if (!overlay || !currentPage) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    imageIndex = itemIndex;
    drag = {
      mode,
      itemIndex,
      startX: event.clientX,
      startY: event.clientY,
      initial: [...currentPage.images[itemIndex].bbox] as [number, number, number, number],
      rect: overlay.getBoundingClientRect(),
      pointerId: event.pointerId,
      target: event.currentTarget as HTMLElement
    };
    drag.target.setPointerCapture?.(event.pointerId);
  }

  function handlePointerMove(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    pendingPoint = { x: event.clientX, y: event.clientY };
    if (dragFrame === null) dragFrame = requestAnimationFrame(() => { dragFrame = null; applyPoint(); });
  }

  function applyPoint() {
    if (!drag || !currentPage || !drag.rect.width || !drag.rect.height) {
      return;
    }
    if (!pendingPoint) return;
    const dx = ((pendingPoint.x - drag.startX) / drag.rect.width) * 1000;
    const dy = ((pendingPoint.y - drag.startY) / drag.rect.height) * 1000;
    pendingPoint = null;
    const [yMin, xMin, yMax, xMax] = drag.initial;
    let next: [number, number, number, number];

    if (drag.mode === "move") {
      const width = xMax - xMin;
      const height = yMax - yMin;
      const left = clamp(xMin + dx, 0, 1000 - width);
      const top = clamp(yMin + dy, 0, 1000 - height);
      next = [Math.round(top), Math.round(left), Math.round(top + height), Math.round(left + width)];
    } else {
      next = [
        yMin,
        xMin,
        Math.round(clamp(yMax + dy, yMin + 10, 1000)),
        Math.round(clamp(xMax + dx, xMin + 10, 1000))
      ];
    }

    const page = { ...currentPage, images: [...currentPage.images] };
    page.images[drag.itemIndex] = { ...page.images[drag.itemIndex], bbox: next };
    replacePage(pageIndex, page);
  }

  function finishDrag() {
    applyPoint();
    if (dragFrame !== null) cancelAnimationFrame(dragFrame);
    dragFrame = null;
    pendingPoint = null;
    if (drag?.target.hasPointerCapture?.(drag.pointerId)) drag.target.releasePointerCapture(drag.pointerId);
    drag = null;
  }

  function adjustWithKeyboard(event: KeyboardEvent, index: number, mode: "move" | "resize") {
    const page = pages[pageIndex];
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key) || !page) return;
    event.preventDefault();
    imageIndex = index;
    const step = event.shiftKey ? 10 : 1;
    const dx = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
    const dy = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
    const [top,left,bottom,right] = page.images[index].bbox;
    let bbox: ImageReviewItem["bbox"];
    if (mode === "resize") bbox = [top,left,clamp(bottom + dy,top + 10,1000),clamp(right + dx,left + 10,1000)];
    else {
      const nextLeft = clamp(left + dx,0,1000 - (right-left));
      const nextTop = clamp(top + dy,0,1000 - (bottom-top));
      bbox = [nextTop,nextLeft,nextTop + bottom-top,nextLeft + right-left];
    }
    replacePage(pageIndex, { ...page, images: page.images.map((image,i) => i === index ? { ...image,bbox } : image) });
  }

  async function close() { if (await confirmDiscardChanges({ dirty: dirtyPages.size > 0, busy: saving })) dispatch("close"); }
  async function reload() { if (documentId && await confirmDiscardChanges({ dirty: dirtyPages.size > 0, busy: saving })) void initialize(documentId); }

  async function submit() {
    if (!documentId) {
      return;
    }
    saving = true;
    const id = documentId;
    const generation = loadGeneration;
    error = "";
    try {
      if (completed) {
        await saveCompletedImageCrops(documentId, pages, expectedUpdatedAt);
      } else {
        await saveImageReviewPages(documentId, pages);
      }
      if (open && documentId === id && generation === loadGeneration) { dirtyPages = new Set(); dispatch("saved"); }
    } catch (err) {
      if (open && documentId === id && generation === loadGeneration) error = err instanceof Error ? err.message : "Failed to save image review.";
    } finally {
      if (documentId === id && generation === loadGeneration) saving = false;
    }
  }
</script>

<svelte:window on:pointermove={handlePointerMove} on:pointerup={finishDrag} on:pointercancel={finishDrag} />

<BaseModal {open} dirty={dirtyPages.size > 0} busy={saving} title={completed ? "Adjust Image Crops" : "Correct Image Bounding Boxes"} size="full" on:close={() => dispatch("close")}>
  <div class="dp-image-review" inert={saving}>
    {#if error}
      <div class="dp-notice error">
        {error}
        {#if completed}<button class="button" type="button" disabled={loading || saving} on:click={reload}>Reload image crops</button>{/if}
      </div>
    {/if}

    <aside>
      {#if completed}<p>Saving updates the images in this document and future exports. Text and captions stay as saved.</p>{/if}
      <label>
        <span>Image sources</span>
        <select size="6" value={String(pageIndex)} on:change={(event) => selectPage(Number(event.currentTarget.value))}>
          {#if loading}
            <option>Loading...</option>
          {:else if !pages.length}
            <option>No images found</option>
          {:else}
            {#each pages as page, index}
              <option value={String(index)}>{page.label || `Page ${page.page_index + 1}`}</option>
            {/each}
          {/if}
        </select>
      </label>

      <label>
        <span>Images</span>
        <select size="6" value={String(imageIndex)} on:change={(event) => (imageIndex = Number(event.currentTarget.value))}>
          {#if !currentPage}
            <option>No page selected</option>
          {:else}
            {#each currentPage.images as item, index}
              {#if item.keep !== false}
                <option value={String(index)}>Image {item.img_idx + 1}</option>
              {/if}
            {/each}
          {/if}
        </select>
      </label>

      {#if !completed}
        <button class="button" type="button" disabled={!currentPage} on:click={addBox}>
          <Icon name="plus" size={16} /> Add Box
        </button>
      {/if}

      {#if completed && currentPage?.source_kind === "saved_image"}
        <p>The original crop coordinates are unavailable. You can trim the saved image, or select its original PDF page to draw a wider crop.</p>
        <label>
          <span>Original PDF page</span>
          <input type="number" min="1" max={pageCount} value={currentPage.source_page_index === undefined ? "" : currentPage.source_page_index + 1} on:change={(event) => selectSourcePage(event.currentTarget.value)} />
          <small>Count from the first page of the PDF. Clear this field to use the saved image.</small>
        </label>
      {/if}

      {#if selectedImage}
        <div class="dp-image-details">
          <label>
            <span>Caption</span>
            <textarea rows="3" readonly={completed} value={selectedImage.caption || ""} on:input={(event) => updateSelected({ caption: event.currentTarget.value })}></textarea>
          </label>
          <div class="dp-bbox-grid">
            <label>Top <input type="number" min="0" max="1000" value={selectedImage.bbox[0]} on:input={(event) => updateSelected({ bbox: [Number(event.currentTarget.value), selectedImage.bbox[1], selectedImage.bbox[2], selectedImage.bbox[3]] })} /></label>
            <label>Left <input type="number" min="0" max="1000" value={selectedImage.bbox[1]} on:input={(event) => updateSelected({ bbox: [selectedImage.bbox[0], Number(event.currentTarget.value), selectedImage.bbox[2], selectedImage.bbox[3]] })} /></label>
            <label>Bottom <input type="number" min="0" max="1000" value={selectedImage.bbox[2]} on:input={(event) => updateSelected({ bbox: [selectedImage.bbox[0], selectedImage.bbox[1], Number(event.currentTarget.value), selectedImage.bbox[3]] })} /></label>
            <label>Right <input type="number" min="0" max="1000" value={selectedImage.bbox[3]} on:input={(event) => updateSelected({ bbox: [selectedImage.bbox[0], selectedImage.bbox[1], selectedImage.bbox[2], Number(event.currentTarget.value)] })} /></label>
          </div>
          <small id="image-edit-help">Coordinates run from 0 to 1000 across the full page. Drag a box or use arrow keys to move it; use its corner handle to resize. Hold Shift for larger steps.</small>
          {#if !validBoxes}<small>Each box needs a positive width and height, with coordinates from 0 to 1000.</small>{/if}
          {#if !completed}<button class="button danger" type="button" on:click={removeBox}>Remove Image</button>{/if}
        </div>
      {/if}
    </aside>

    <section class="dp-image-canvas">
      {#if currentPage}
        <div class="dp-image-overlay-wrap">
          <img src={currentPage.image_url} alt={currentPage.label || `Page ${currentPage.page_index + 1}`} />
          <div bind:this={overlay} class="dp-image-overlay">
            {#each currentPage.images as item, index}
              {#if item.keep !== false}
                <div
                  class:selected={index === imageIndex}
                  class="dp-image-box"
                  style={boxStyle(item)}
                >
                  <button type="button" class="dp-image-box-move" aria-label={`Select image ${item.img_idx + 1}`} aria-describedby="image-edit-help" title="Move image: drag or use arrow keys" on:pointerdown={(event) => beginDrag(event,index,"move")} on:click={() => imageIndex = index} on:keydown={(event) => adjustWithKeyboard(event,index,"move")}></button>
                  <button
                    type="button"
                    class="dp-image-resize"
                    aria-label={`Resize image ${item.img_idx + 1}`}
                    title="Resize image: drag or use arrow keys"
                    aria-describedby="image-edit-help"
                    on:pointerdown={(event) => beginDrag(event, index, "resize")}
                    on:click={() => imageIndex = index}
                    on:keydown={(event) => adjustWithKeyboard(event,index,"resize")}
                  ><Icon name="crop" size={12} /></button>
                </div>
              {/if}
            {/each}
          </div>
        </div>
      {:else}
        <div class="dp-empty-state"><Icon name="image" size={20} /><span>No page selected.</span></div>
      {/if}
    </section>
  </div>

  <div slot="footer" class="dp-modal-footer">
    <button class="button ghost" type="button" disabled={saving} on:click={close}>Cancel</button>
    <button class="button success" type="button" disabled={saving || loading || !pages.length || !validBoxes || sourcePageInvalid || (completed && !expectedUpdatedAt)} on:click={submit}>
      {#if saving}<Icon name="refresh" size={16} class="dp-spin" />{/if}
      {completed ? "Save image crops" : "Save and Continue"}
    </button>
  </div>
</BaseModal>
