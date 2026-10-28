<script lang="ts">
  interface Props { src: string; crop?: number[] | null; busy?: boolean; }
  let { src, crop = $bindable(null), busy = false }: Props = $props();
  const labels = ["Left", "Top", "Right", "Bottom"];
  const full = [0, 0, 1, 1];
  let values = $derived(crop || full);
  function edge(index: number, percent: string) {
    const next = [...values];
    next[index] = Number(percent) / 100;
    crop = next;
  }
</script>

<fieldset disabled={busy}>
  <legend>Cover crop</legend>
  <p>The outlined area becomes the cover. Coordinates follow the visible PDF page, including its existing crop and rotation.</p>
  <div class="preview">
    <img {src} alt="Source PDF page for choosing the cover crop" />
    <div class="outline" aria-hidden="true" style={`left:${values[0] * 100}%;top:${values[1] * 100}%;right:${(1 - values[2]) * 100}%;bottom:${(1 - values[3]) * 100}%;`}></div>
  </div>
  <div class="edges">{#each labels as label, index}
    <label>{label} (%)<input type="number" min="0" max="100" step="0.1" value={Number((values[index] * 100).toFixed(4))} oninput={(event) => edge(index, event.currentTarget.value)} /></label>
  {/each}</div>
  <div class="presets">
    <button type="button" onclick={() => crop = null}>Full visible page</button>
    <button type="button" onclick={() => crop = [0, 0, .5, 1]}>Left half</button>
    <button type="button" onclick={() => crop = [.5, 0, 1, 1]}>Right half</button>
  </div>
  {#if values[0] < 0 || values[1] < 0 || values[2] > 1 || values[3] > 1 || values[2] - values[0] < .01 || values[3] - values[1] < .01}<p role="alert">Choose a crop at least 1% wide and high, within the page.</p>{/if}
</fieldset>

<style>
  fieldset { min-width: 0; padding: .75rem; border: 1px solid var(--border-color, #8888); border-radius: .5rem; }
  p { font-size: .85rem; }
  .preview { position: relative; width: fit-content; max-width: 100%; max-height: 18rem; margin: .75rem auto; overflow: hidden; }
  img { display: block; max-width: 100%; max-height: 18rem; }
  .outline { position: absolute; border: 2px solid #2563eb; box-shadow: 0 0 0 30rem #0006; pointer-events: none; }
  .edges { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .5rem; }
  label { display: grid; gap: .25rem; }
  input { width: 100%; box-sizing: border-box; font: inherit; }
  .presets { display: flex; flex-wrap: wrap; gap: .5rem; margin-block-start: .75rem; }
  button { font: inherit; padding: .35rem .6rem; }
  input:focus-visible, button:focus-visible { outline: 2px solid var(--accent-color, #2563eb); outline-offset: 2px; }
</style>
