<script lang="ts">
  import { createEventDispatcher, onDestroy } from "svelte";
  import type { CropBox } from "../lib/types";
  import { clamp } from "../lib/utils";

  export let title = "";
  export let imageSrc = "";
  export let cropBox: CropBox | null = null;
  export let suggestedBox: CropBox | null = null;
  export let meta = "";
  export let emptyMessage = "No page selected.";

  type EditAction = "move" | "resize";
  type ResizeHandle = "nw" | "ne" | "sw" | "se";

  const dispatch = createEventDispatcher<{ cropBoxChange: { box: CropBox; action: EditAction } }>();
  const handleSize = 12;
  const minCropSize = 0.015;

  let canvas: HTMLCanvasElement | null = null;
  let context: CanvasRenderingContext2D | null = null;
  let image: HTMLImageElement | null = null;
  let status = "";
  let loadId = 0;
  let loadedSrc = "";
  let frame: number | null = null;
  let imageLoadTimeout: ReturnType<typeof setTimeout> | null = null;
  let interaction: EditAction | "draw" | null = null;
  let resizeHandle: ResizeHandle | null = null;
  let dragOffsetX = 0;
  let dragOffsetY = 0;
  let dragTemplate: CropBox | null = null;
  let dragBox: CropBox | null = null;
  let drawStart: [number, number] | null = null;
  let canvasCursor = "default";

  $: if (canvas && !context) {
    context = canvas.getContext("2d");
  }

  $: if (canvas && context) {
    loadImage(imageSrc, emptyMessage);
  }

  $: if (image && !interaction) {
    scheduleDrawFor(cropBox, suggestedBox);
  }

  onDestroy(() => {
    if (frame !== null) {
      cancelAnimationFrame(frame);
    }
    if (imageLoadTimeout) {
      clearTimeout(imageLoadTimeout);
    }
  });

  async function loadImage(src: string, fallbackMessage = "") {
    if (src && image && loadedSrc === src) {
      status = "";
      return;
    }
    const id = ++loadId;
    image = null;
    dragBox = null;
    interaction = null;
    if (imageLoadTimeout) {
      clearTimeout(imageLoadTimeout);
      imageLoadTimeout = null;
    }
    if (!canvas || !context) {
      return;
    }
    if (!src) {
      loadedSrc = "";
      canvas.width = 0;
      canvas.height = 0;
      status = fallbackMessage || "No page selected.";
      return;
    }

    status = "Loading page...";
    const nextImage = new Image();
    nextImage.decoding = "async";
    const finishLoad = () => {
      if (id !== loadId || !canvas || !context) {
        return;
      }
      if (imageLoadTimeout) {
        clearTimeout(imageLoadTimeout);
        imageLoadTimeout = null;
      }
      image = nextImage;
      loadedSrc = src;
      canvas.width = nextImage.naturalWidth || nextImage.width;
      canvas.height = nextImage.naturalHeight || nextImage.height;
      status = "";
      draw();
    };
    nextImage.onload = finishLoad;
    nextImage.onerror = () => {
      if (id === loadId) {
        if (imageLoadTimeout) {
          clearTimeout(imageLoadTimeout);
          imageLoadTimeout = null;
        }
        status = "Failed to load page preview.";
      }
    };
    imageLoadTimeout = setTimeout(() => {
      if (id === loadId) {
        status = "Page preview did not finish loading.";
      }
    }, 10_000);
    nextImage.src = src;
    if (nextImage.complete && (nextImage.naturalWidth || nextImage.width)) {
      finishLoad();
    }
  }

  function activeBox(): CropBox | null {
    return dragBox || cropBox || suggestedBox;
  }

  function scheduleDraw() {
    if (frame !== null) {
      return;
    }
    frame = requestAnimationFrame(() => {
      frame = null;
      draw();
    });
  }

  function scheduleDrawFor(_cropBox: CropBox | null, _suggestedBox: CropBox | null) {
    scheduleDraw();
  }

  function draw() {
    if (!canvas || !context || !image) {
      return;
    }

    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0);

    if (suggestedBox && cropBox) {
      drawBoundary(suggestedBox, {
        fill: "rgba(31, 108, 161, 0.08)",
        stroke: "#1f6ca1",
        dash: [10, 5],
        lineWidth: 3
      }, false);
    }

    const box = activeBox();
    if (!box) {
      return;
    }

    dimToBox(box);
    drawBoundary(box, cropBox || dragBox
      ? {
          fill: "rgba(40, 167, 69, 0.16)",
          stroke: "#28a745",
          dash: [6, 5],
          lineWidth: 2
        }
      : {
          fill: "rgba(31, 108, 161, 0.12)",
          stroke: "#1f6ca1",
          dash: [10, 5],
          lineWidth: 3
        },
      true
    );
  }

  function dimToBox(box: CropBox) {
    if (!canvas || !context || !image) {
      return;
    }

    const [x0, y0, x1, y1] = box;
    const x = x0 * canvas.width;
    const y = y0 * canvas.height;
    const width = (x1 - x0) * canvas.width;
    const height = (y1 - y0) * canvas.height;

    context.fillStyle = "rgba(0, 0, 0, 0.58)";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, x, y, width, height, x, y, width, height);
  }

  function drawBoundary(
    box: CropBox,
    style: { fill: string; stroke: string; dash: number[]; lineWidth: number },
    handles: boolean
  ) {
    if (!canvas || !context) {
      return;
    }

    const [x0, y0, x1, y1] = box;
    const x = x0 * canvas.width;
    const y = y0 * canvas.height;
    const width = (x1 - x0) * canvas.width;
    const height = (y1 - y0) * canvas.height;

    context.fillStyle = style.fill;
    context.fillRect(x, y, width, height);
    context.strokeStyle = style.stroke;
    context.lineWidth = style.lineWidth;
    context.setLineDash(style.dash);
    context.shadowColor = "rgba(255, 255, 255, 0.9)";
    context.shadowBlur = 2;
    context.strokeRect(x, y, width, height);
    context.shadowBlur = 0;
    context.setLineDash([]);

    if (handles) {
      drawResizeHandles(box, style.stroke);
    }
  }

  function drawResizeHandles(box: CropBox, color: string) {
    if (!canvas || !context) {
      return;
    }
    const points = [
      [box[0] * canvas.width, box[1] * canvas.height],
      [box[2] * canvas.width, box[1] * canvas.height],
      [box[0] * canvas.width, box[3] * canvas.height],
      [box[2] * canvas.width, box[3] * canvas.height]
    ];
    context.fillStyle = "#fff";
    context.strokeStyle = color;
    context.lineWidth = 2;
    for (const [x, y] of points) {
      context.fillRect(x - handleSize / 2, y - handleSize / 2, handleSize, handleSize);
      context.strokeRect(x - handleSize / 2, y - handleSize / 2, handleSize, handleSize);
    }
  }

  function canvasPoint(event: PointerEvent): [number, number] | null {
    if (!canvas) {
      return null;
    }
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      return null;
    }
    return [
      clamp((event.clientX - rect.left) * (canvas.width / rect.width), 0, canvas.width),
      clamp((event.clientY - rect.top) * (canvas.height / rect.height), 0, canvas.height)
    ];
  }

  function boxPixels(box: CropBox): [number, number, number, number] | null {
    if (!canvas) {
      return null;
    }
    return [
      box[0] * canvas.width,
      box[1] * canvas.height,
      box[2] * canvas.width,
      box[3] * canvas.height
    ];
  }

  function cornerHandle(point: [number, number], box: CropBox): ResizeHandle | null {
    const pixels = boxPixels(box);
    if (!pixels) {
      return null;
    }
    const [x0, y0, x1, y1] = pixels;
    const handles: Array<[ResizeHandle, number, number]> = [
      ["nw", x0, y0],
      ["ne", x1, y0],
      ["sw", x0, y1],
      ["se", x1, y1]
    ];
    for (const [handle, x, y] of handles) {
      if (Math.abs(point[0] - x) <= handleSize && Math.abs(point[1] - y) <= handleSize) {
        return handle;
      }
    }
    return null;
  }

  function cursorForHandle(handle: ResizeHandle | null): string {
    if (handle === "nw" || handle === "se") {
      return "nwse-resize";
    }
    if (handle === "ne" || handle === "sw") {
      return "nesw-resize";
    }
    return "move";
  }

  function translatedBox(template: CropBox, x: number, y: number): CropBox | null {
    if (!canvas) {
      return null;
    }
    const width = template[2] - template[0];
    const height = template[3] - template[1];
    const x0 = clamp(x / canvas.width, 0, Math.max(0, 1 - width));
    const y0 = clamp(y / canvas.height, 0, Math.max(0, 1 - height));
    return [x0, y0, x0 + width, y0 + height];
  }

  function resizedBox(template: CropBox, handle: ResizeHandle, point: [number, number]): CropBox | null {
    if (!canvas) {
      return null;
    }
    const x = clamp(point[0] / canvas.width, 0, 1);
    const y = clamp(point[1] / canvas.height, 0, 1);
    let x0 = template[0];
    let y0 = template[1];
    let x1 = template[2];
    let y1 = template[3];

    if (handle === "nw" || handle === "sw") {
      x0 = Math.min(x, x1 - minCropSize);
    } else {
      x1 = Math.max(x, x0 + minCropSize);
    }
    if (handle === "nw" || handle === "ne") {
      y0 = Math.min(y, y1 - minCropSize);
    } else {
      y1 = Math.max(y, y0 + minCropSize);
    }
    return [
      clamp(x0, 0, 1 - minCropSize),
      clamp(y0, 0, 1 - minCropSize),
      clamp(x1, minCropSize, 1),
      clamp(y1, minCropSize, 1)
    ];
  }

  function drawnBox(start: [number, number], point: [number, number]): CropBox | null {
    if (!canvas) {
      return null;
    }
    const x0 = clamp(Math.min(start[0], point[0]) / canvas.width, 0, 1);
    const y0 = clamp(Math.min(start[1], point[1]) / canvas.height, 0, 1);
    const x1 = clamp(Math.max(start[0], point[0]) / canvas.width, 0, 1);
    const y1 = clamp(Math.max(start[1], point[1]) / canvas.height, 0, 1);
    if (x1 - x0 < minCropSize || y1 - y0 < minCropSize) {
      return null;
    }
    return [x0, y0, x1, y1];
  }

  function emitBox(box: CropBox, action: EditAction) {
    dragBox = box;
    dispatch("cropBoxChange", { box, action });
    scheduleDraw();
  }

  function moveTo(point: [number, number]) {
    if (!dragTemplate) {
      return;
    }
    if (interaction === "move") {
      const nextBox = translatedBox(dragTemplate, point[0] - dragOffsetX, point[1] - dragOffsetY);
      if (nextBox) {
        emitBox(nextBox, "move");
      }
      return;
    }
    if (interaction === "resize" && resizeHandle) {
      const nextBox = resizedBox(dragTemplate, resizeHandle, point);
      if (nextBox) {
        emitBox(nextBox, "resize");
      }
      return;
    }
    if (interaction === "draw" && drawStart) {
      const nextBox = drawnBox(drawStart, point);
      if (nextBox) {
        emitBox(nextBox, "resize");
      }
    }
  }

  function begin(event: PointerEvent) {
    if (!image || !canvas) {
      return;
    }
    const point = canvasPoint(event);
    if (!point) {
      return;
    }
    const template = cropBox || suggestedBox;
    if (!template) {
      interaction = "draw";
      drawStart = point;
      canvas.setPointerCapture(event.pointerId);
      return;
    }
    const pixels = boxPixels(template);
    if (!pixels) {
      return;
    }

    const handle = cornerHandle(point, template);
    if (handle) {
      interaction = "resize";
      resizeHandle = handle;
      dragTemplate = [...template] as CropBox;
      canvas.setPointerCapture(event.pointerId);
      moveTo(point);
      return;
    }

    const [x0, y0, x1, y1] = pixels;
    const inside = point[0] >= x0 && point[0] <= x1 && point[1] >= y0 && point[1] <= y1;
    dragOffsetX = inside ? point[0] - x0 : (x1 - x0) / 2;
    dragOffsetY = inside ? point[1] - y0 : (y1 - y0) / 2;
    dragTemplate = [...template] as CropBox;
    interaction = "move";
    canvas.setPointerCapture(event.pointerId);
    moveTo(point);
  }

  function move(event: PointerEvent) {
    const point = canvasPoint(event);
    if (!point) {
      return;
    }
    if (interaction) {
      moveTo(point);
      return;
    }
    const box = activeBox();
    const handle = box ? cornerHandle(point, box) : null;
    const pixels = box ? boxPixels(box) : null;
    const inside = pixels
      ? point[0] >= pixels[0] && point[0] <= pixels[2] && point[1] >= pixels[1] && point[1] <= pixels[3]
      : false;
    canvasCursor = handle ? cursorForHandle(handle) : inside || box ? "move" : "crosshair";
  }

  function finish(event?: PointerEvent) {
    if (!interaction || !canvas) {
      return;
    }
    if (event && canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
    interaction = null;
    resizeHandle = null;
    dragTemplate = null;
    drawStart = null;
    dragBox = null;
    scheduleDraw();
  }

  function useSuggested() {
    if (suggestedBox) {
      dispatch("cropBoxChange", { box: [...suggestedBox] as CropBox, action: "resize" });
      scheduleDraw();
    }
  }
</script>

<article class="dp-crop-preview-card dp-crop-page-preview-card">
  <header>
    <div class="dp-crop-preview-title">
      <strong>{title}</strong>
      <small>{meta}</small>
    </div>
    <div class="dp-crop-preview-actions">
      <slot name="actions"></slot>
      {#if suggestedBox}
        <button class="dp-icon-btn" type="button" title="Use detected text boundary" on:click={useSuggested}>
          <i class="fa-solid fa-wand-magic-sparkles"></i>
        </button>
      {/if}
    </div>
  </header>
  <div class="dp-crop-canvas-shell">
    {#if status}
      <p>{status}</p>
    {/if}
    <canvas
      bind:this={canvas}
      class:hidden={!!status}
      style={`cursor:${canvasCursor}`}
      on:pointerdown={begin}
      on:pointermove={move}
      on:pointerup={finish}
      on:pointercancel={finish}
    ></canvas>
  </div>
</article>
