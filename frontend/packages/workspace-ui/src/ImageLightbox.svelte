<script lang="ts">
  import { tick } from "svelte";
  import type { LightboxImage } from "./types";

  export let open = false;
  export let images: LightboxImage[] = [];
  export let activeIndex = 0;
  export let onClose: () => void = () => undefined;
  export let onChange: (index: number) => void = () => undefined;

  let dialog: HTMLDivElement;
  let wasOpen = false;
  let zoom = 1;
  let rotation = 0;

  $: safeIndex = images.length
    ? Math.max(0, Math.min(activeIndex, images.length - 1))
    : 0;
  $: current = images[safeIndex] ?? null;
  $: if (open && !wasOpen) {
    wasOpen = true;
    zoom = 1;
    rotation = 0;
    void focusDialog();
  } else if (!open) {
    wasOpen = false;
  }

  async function focusDialog(): Promise<void> {
    await tick();
    dialog?.focus();
  }

  function close(): void {
    onClose();
  }

  function select(index: number): void {
    if (!images.length) return;
    const next = (index + images.length) % images.length;
    activeIndex = next;
    zoom = 1;
    rotation = 0;
    onChange(next);
  }

  function changeZoom(delta: number): void {
    zoom = Math.max(0.5, Math.min(4, Number((zoom + delta).toFixed(2))));
  }

  function resetTransform(): void {
    zoom = 1;
    rotation = 0;
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (!open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key === "ArrowLeft" && images.length > 1) {
      event.preventDefault();
      event.stopPropagation();
      select(safeIndex - 1);
      return;
    }
    if (event.key === "ArrowRight" && images.length > 1) {
      event.preventDefault();
      event.stopPropagation();
      select(safeIndex + 1);
      return;
    }
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      changeZoom(0.25);
      return;
    }
    if (event.key === "-") {
      event.preventDefault();
      changeZoom(-0.25);
      return;
    }
    if (event.key === "0") {
      event.preventDefault();
      resetTransform();
      return;
    }
    if (event.key.toLowerCase() === "r") {
      event.preventDefault();
      rotation = (rotation + 90) % 360;
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open && current}
  <div
    class="iarf-lightbox"
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && close()}
  >
    <div
      bind:this={dialog}
      class="iarf-lightbox__dialog"
      role="dialog"
      aria-label={current.title || current.alt || "Image preview"}
      aria-modal="true"
      tabindex="-1"
    >
      <header>
        <div>
          <strong>{current.title || current.alt || "Image preview"}</strong>
          {#if images.length > 1}
            <span>{safeIndex + 1} of {images.length}</span>
          {/if}
        </div>
        <div class="iarf-lightbox__tools" aria-label="Image controls">
          <button type="button" aria-label="Zoom out" title="Zoom out (−)" onclick={() => changeZoom(-0.25)}>−</button>
          <button type="button" aria-label="Reset zoom" title="Reset zoom (0)" onclick={resetTransform}>
            {Math.round(zoom * 100)}%
          </button>
          <button type="button" aria-label="Zoom in" title="Zoom in (+)" onclick={() => changeZoom(0.25)}>+</button>
          <button type="button" aria-label="Rotate image" title="Rotate (R)" onclick={() => rotation = (rotation + 90) % 360}>↻</button>
          <a
            href={current.downloadUrl || current.src}
            aria-label="Open original image"
            title="Open original image"
            target="_blank"
            rel="noreferrer"
          >↗</a>
          <button type="button" aria-label="Close image preview" title="Close (Esc)" onclick={close}>×</button>
        </div>
      </header>

      <div class="iarf-lightbox__stage">
        {#if images.length > 1}
          <button
            type="button"
            class="iarf-lightbox__previous"
            aria-label="Previous image"
            onclick={() => select(safeIndex - 1)}
          >‹</button>
        {/if}
        <img
          src={current.src}
          alt={current.alt || current.title || ""}
          draggable="false"
          style={`transform: scale(${zoom}) rotate(${rotation}deg)`}
        />
        {#if images.length > 1}
          <button
            type="button"
            class="iarf-lightbox__next"
            aria-label="Next image"
            onclick={() => select(safeIndex + 1)}
          >›</button>
        {/if}
      </div>

      <footer>
        <span>←/→ navigate · +/− zoom · R rotate · Esc close</span>
      </footer>
    </div>
  </div>
{/if}

<style>
  .iarf-lightbox,
  .iarf-lightbox * {
    box-sizing: border-box;
  }

  .iarf-lightbox {
    position: fixed;
    z-index: 10000;
    inset: 0;
    display: grid;
    place-items: center;
    padding: clamp(0.5rem, 2vw, 1.5rem);
    background: rgba(18, 17, 15, 0.88);
    backdrop-filter: blur(8px);
  }

  .iarf-lightbox__dialog {
    width: min(92rem, 100%);
    height: min(58rem, 100%);
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.22);
    background: #171614;
    color: #f8f5ef;
    box-shadow: 0 2rem 7rem rgba(0, 0, 0, 0.55);
  }

  .iarf-lightbox__dialog:focus {
    outline: none;
  }

  .iarf-lightbox header {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.15);
    padding: 0.55rem 0.65rem 0.55rem 0.9rem;
  }

  .iarf-lightbox header > div:first-child {
    min-width: 0;
    display: grid;
    gap: 0.08rem;
  }

  .iarf-lightbox header strong {
    overflow: hidden;
    font-size: 0.78rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .iarf-lightbox header span,
  .iarf-lightbox footer {
    color: rgba(255, 255, 255, 0.6);
    font-size: 0.62rem;
  }

  .iarf-lightbox__tools {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
  }

  .iarf-lightbox__tools button,
  .iarf-lightbox__tools a,
  .iarf-lightbox__previous,
  .iarf-lightbox__next {
    min-width: 2.25rem;
    min-height: 2.25rem;
    display: grid;
    place-items: center;
    border: 1px solid rgba(255, 255, 255, 0.22);
    border-right: 0;
    border-radius: 0;
    padding: 0.35rem;
    background: transparent;
    color: inherit;
    font: inherit;
    text-decoration: none;
    cursor: pointer;
  }

  .iarf-lightbox__tools > :last-child {
    border-right: 1px solid rgba(255, 255, 255, 0.22);
  }

  .iarf-lightbox__tools button:hover,
  .iarf-lightbox__tools a:hover,
  .iarf-lightbox__previous:hover,
  .iarf-lightbox__next:hover {
    background: rgba(255, 255, 255, 0.12);
  }

  .iarf-lightbox button:focus-visible,
  .iarf-lightbox a:focus-visible {
    outline: 2px solid #d4b374;
    outline-offset: -3px;
  }

  .iarf-lightbox__stage {
    position: relative;
    min-height: 0;
    display: grid;
    place-items: center;
    overflow: auto;
    padding: 2.75rem;
    background:
      linear-gradient(45deg, #1e1d1a 25%, transparent 25%),
      linear-gradient(-45deg, #1e1d1a 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #1e1d1a 75%),
      linear-gradient(-45deg, transparent 75%, #1e1d1a 75%),
      #181715;
    background-position: 0 0, 0 0.5rem, 0.5rem -0.5rem, -0.5rem 0;
    background-size: 1rem 1rem;
  }

  .iarf-lightbox__stage img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    transform-origin: center;
    transition: transform 140ms ease;
    user-select: none;
  }

  .iarf-lightbox__previous,
  .iarf-lightbox__next {
    position: fixed;
    z-index: 1;
    top: 50%;
    width: 2.8rem;
    height: 4rem;
    border: 1px solid rgba(255, 255, 255, 0.24);
    background: rgba(0, 0, 0, 0.4);
    font-size: 2rem;
    transform: translateY(-50%);
  }

  .iarf-lightbox__previous {
    left: clamp(0.75rem, 3vw, 3rem);
  }

  .iarf-lightbox__next {
    right: clamp(0.75rem, 3vw, 3rem);
  }

  .iarf-lightbox footer {
    padding: 0.42rem 0.8rem;
    text-align: center;
  }

  @media (max-width: 620px) {
    .iarf-lightbox {
      padding: 0;
    }

    .iarf-lightbox__dialog {
      width: 100%;
      height: 100%;
    }

    .iarf-lightbox header {
      align-items: flex-start;
    }

    .iarf-lightbox header > div:first-child,
    .iarf-lightbox footer {
      display: none;
    }

    .iarf-lightbox__tools {
      width: 100%;
      justify-content: flex-end;
    }

    .iarf-lightbox__stage {
      padding: 1rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .iarf-lightbox__stage img {
      transition: none;
    }
  }
</style>
