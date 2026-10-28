<script lang="ts">
  import { createEventDispatcher, onDestroy } from "svelte";
  import type { CropBox } from "../lib/types";

  export let title = "";
  export let imageSrc = "";
  export let cropBox: CropBox | null = null;
  export let suggestedBox: CropBox | null = null;
  export let meta = "";
  export let loading = false;
  export let emptyMessage = "";

  const dispatch = createEventDispatcher<{ cropBoxChange: CropBox }>();

  let canvas: HTMLCanvasElement | null = null;
  let context: CanvasRenderingContext2D | null = null;
  let image: HTMLImageElement | null = null;
  let status = "Generating preview...";
  let loadId = 0;
  let drawing = false;
  let startX = 0;
  let startY = 0;
  let currentX = 0;
  let currentY = 0;
  let frame: number | null = null;
  let loadedSrc = "";
  let imageLoadTimeout: ReturnType<typeof setTimeout> | null = null;

  $: if (canvas && !context) {
    context = canvas.getContext("2d");
  }

  $: if (canvas && context) {
    loadImage(imageSrc, loading, emptyMessage);
  }

  // Empty stacks never reach image loading. Keep their visible status derived
  // directly from the preview response so a left/right stack with no assigned
  // pages cannot remain stuck on the component's initial loading message.
  $: if (!imageSrc) {
    status = loading
      ? "Generating preview..."
      : emptyMessage || "Preview is not available for this stack.";
  }

  onDestroy(() => {
    if (frame !== null) {
      cancelAnimationFrame(frame);
    }
    if (imageLoadTimeout) {
      clearTimeout(imageLoadTimeout);
    }
  });

  async function loadImage(src: string, isLoading = false, fallbackMessage = "") {
    if (src && image && loadedSrc === src) {
      status = "";
      return;
    }
    const id = ++loadId;
    image = null;
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
      status = isLoading ? "Generating preview..." : fallbackMessage || "Preview is not available for this stack.";
      return;
    }

    status = "Loading preview...";
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
        status = "Failed to load crop preview image.";
      }
    };
    imageLoadTimeout = setTimeout(() => {
      if (id === loadId) {
        status = "Preview image did not finish loading.";
      }
    }, 10_000);
    nextImage.src = src;
    if (nextImage.complete && (nextImage.naturalWidth || nextImage.width)) {
      finishLoad();
    }
  }

  $: if (image && !drawing) {
    scheduleDrawFor(cropBox, suggestedBox);
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

    if (suggestedBox) {
      drawBoundary(suggestedBox, {
        fill: "rgba(31, 108, 161, 0.08)",
        stroke: "#1f6ca1",
        dash: [10, 5],
        lineWidth: 3
      });
    }

    const box = drawing
      ? [
          Math.min(startX, currentX) / canvas.width,
          Math.min(startY, currentY) / canvas.height,
          Math.max(startX, currentX) / canvas.width,
          Math.max(startY, currentY) / canvas.height
        ]
      : cropBox;

    if (!box) {
      return;
    }

    dimToBox(box);
    drawBoundary(box, {
      fill: "rgba(40, 167, 69, 0.16)",
      stroke: "#28a745",
      dash: [6, 5],
      lineWidth: 2
    });
  }

  function dimToBox(box: CropBox | number[]) {
    if (!canvas || !context || !image) {
      return;
    }

    const [x0, y0, x1, y1] = box;
    const x = x0 * canvas.width;
    const y = y0 * canvas.height;
    const width = (x1 - x0) * canvas.width;
    const height = (y1 - y0) * canvas.height;

    context.fillStyle = "rgba(0, 0, 0, 0.6)";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, x, y, width, height, x, y, width, height);
  }

  function drawBoundary(box: CropBox | number[], style: { fill: string; stroke: string; dash: number[]; lineWidth: number }) {
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
      Math.max(0, Math.min(canvas.width, (event.clientX - rect.left) * (canvas.width / rect.width))),
      Math.max(0, Math.min(canvas.height, (event.clientY - rect.top) * (canvas.height / rect.height)))
    ];
  }

  function begin(event: PointerEvent) {
    if (!image || !canvas) {
      return;
    }
    const point = canvasPoint(event);
    if (!point) {
      return;
    }
    [startX, startY] = point;
    [currentX, currentY] = point;
    drawing = true;
    canvas.setPointerCapture(event.pointerId);
    scheduleDraw();
  }

  function move(event: PointerEvent) {
    if (!drawing) {
      return;
    }
    const point = canvasPoint(event);
    if (!point) {
      return;
    }
    [currentX, currentY] = point;
    scheduleDraw();
  }

  function finish(event?: PointerEvent) {
    if (!drawing || !canvas) {
      return;
    }
    if (event && canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
    drawing = false;
    if (Math.abs(currentX - startX) > 20 && Math.abs(currentY - startY) > 20) {
      cropBox = [
        Math.min(startX, currentX) / canvas.width,
        Math.min(startY, currentY) / canvas.height,
        Math.max(startX, currentX) / canvas.width,
        Math.max(startY, currentY) / canvas.height
      ];
      dispatch("cropBoxChange", cropBox);
    }
    draw();
  }

  function useSuggested() {
    if (suggestedBox) {
      cropBox = [...suggestedBox] as CropBox;
      dispatch("cropBoxChange", cropBox);
      scheduleDraw();
    }
  }
</script>

<article class="dp-crop-preview-card">
  <header>
    <div class="dp-crop-preview-title">
      <strong>{title}</strong>
      <small>{meta}</small>
    </div>
    {#if suggestedBox}
      <button class="dp-icon-btn" type="button" title="Use detected text boundary" on:click={useSuggested}>
        <i class="fa-solid fa-wand-magic-sparkles"></i>
      </button>
    {/if}
  </header>
  <div class="dp-crop-canvas-shell">
    {#if status}
      <p>{status}</p>
    {/if}
    <canvas
      bind:this={canvas}
      class:hidden={!!status}
      on:pointerdown={begin}
      on:pointermove={move}
      on:pointerup={finish}
      on:pointercancel={finish}
    ></canvas>
  </div>
</article>
