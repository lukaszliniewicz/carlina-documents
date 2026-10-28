<script lang="ts">
  import type { EpubImageMode } from "./epub.ts";

  interface Props { mode?: EpubImageMode; busy?: boolean; }
  let { mode = $bindable("jpeg"), busy = false }: Props = $props();
</script>

<fieldset class="epub-image-format" disabled={busy}>
  <legend>Images</legend>
  <label>Image format
    <select bind:value={mode}>
      <option value="jpeg">JPEG (default)</option>
      <option value="auto">Auto</option>
      <option value="webp">WebP</option>
    </select>
  </label>
  {#if mode === "auto"}
    <p>Optimizes covers and confirmed photographs when enabled. Other images keep their original format.</p>
  {:else if mode === "jpeg"}
    <p>JPEG quality 90 for opaque images, including diagrams. Preserves SVG, transparency and animation.</p>
  {:else}
    <p>WebP quality 84, with lossless compression for transparency and limited-colour artwork. Older EPUB readers may lack WebP support.</p>
  {/if}
</fieldset>

<style>
  fieldset { min-width: 0; padding: .75rem; border: 1px solid var(--border-color, #8888); border-radius: .5rem; }
  fieldset.epub-image-format label { display: grid; gap: .35rem; }
  select { box-sizing: border-box; width: 100%; font: inherit; color: inherit; background: var(--surface-color, transparent); border: 1px solid var(--border-color, #8888); border-radius: .3rem; padding: .5rem; }
  select:focus-visible { outline: 2px solid var(--accent-color, #3b82f6); outline-offset: 2px; }
  p { margin: .5rem 0 0; font-size: .85rem; line-height: 1.5; }
</style>
