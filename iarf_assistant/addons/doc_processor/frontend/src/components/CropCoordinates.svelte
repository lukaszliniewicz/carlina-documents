<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import type { CropBox } from "../lib/types";
  export let box: CropBox | null = null;
  const labels = ["Left", "Top", "Right", "Bottom"];
  const dispatch = createEventDispatcher<{ change: CropBox }>();
  let invalid = false;
  function change(index: number, raw: string) {
    const value = Number(raw);
    const next = [...(box || [0, 0, 1, 1])] as CropBox;
    next[index] = value / 100;
    invalid = !raw.trim() || !Number.isFinite(value) || next.some(n => n < 0 || n > 1) || next[0] >= next[2] || next[1] >= next[3];
    if (!invalid) dispatch("change", next);
  }
</script>

<details class="crop-coordinates">
  <summary>Crop coordinates (%)</summary>
  <div>{#each labels as label,index}<label>{label}<input aria-label={`Crop ${label.toLowerCase()} (%)`} type="number" min="0" max="100" step="0.1" value={Math.round((box || [0,0,1,1])[index] * 1000) / 10} on:change={(event) => change(index,event.currentTarget.value)} /></label>{/each}</div>
  {#if invalid}<small role="alert">Use values from 0 to 100, with Left before Right and Top before Bottom.</small>{/if}
</details>

<style>
  details { padding:.4rem .65rem; font-size:.8rem; }
  summary { cursor:pointer; }
  div { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.4rem; margin-top:.4rem; }
  label { display:grid; gap:.2rem; }
  input { min-width:0; width:100%; padding:.25rem; box-sizing:border-box; color:inherit; background:var(--color-surface,#fff); border:1px solid var(--color-rule,#ccc); }
</style>
