<script lang="ts">
  import { createEventDispatcher } from "svelte";

  export let html = "";
  export let label = "Edit text";
  const dispatch = createEventDispatcher<{ change: string }>();
  let lastHtml: string | null = null;
  let root: HTMLElement;
  let runs: { node: Text; text: string; context: string }[] = [];

  $: if (html !== lastHtml) {
    lastHtml = html;
    root = new DOMParser().parseFromString(html, "text/html").body;
    const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const next: typeof runs = [];
    let node: Node | null;
    while ((node = walker.nextNode())) {
      if (!node.textContent?.trim()) continue;
      const tag = node.parentElement?.tagName.toLowerCase();
      next.push({ node: node as Text, text: node.textContent, context: tag === "a" ? "Link text" : ["em", "i"].includes(tag || "") ? "Italic text" : ["strong", "b"].includes(tag || "") ? "Bold text" : "" });
    }
    runs = next;
  }

  function change(index: number, value: string) {
    runs[index].node.data = value;
    runs[index].text = value;
    lastHtml = root.innerHTML;
    dispatch("change", lastHtml);
  }
</script>

<div class="dp-text-runs">
  {#each runs as run, index}
    <label>
      {#if run.context}<small>{run.context}</small>{/if}
      <textarea aria-label={runs.length === 1 ? label : `${label}, part ${index + 1}${run.context ? ` (${run.context})` : ""}`} rows={run.text.length > 160 ? 3 : 1} value={run.text} on:input={(event) => change(index, event.currentTarget.value)}></textarea>
    </label>
  {/each}
  {#if !runs.length}<small>No text to correct in this block.</small>{/if}
</div>

<style>
  .dp-text-runs { display:grid; gap:.4rem; width:100%; }
  label { display:grid; gap:.2rem; }
  small { color:var(--color-ink-muted,#667); }
  textarea { width:100%; min-height:2.5rem; resize:vertical; box-sizing:border-box; font:inherit; line-height:1.5; padding:.55rem; color:inherit; background:var(--color-surface,#fff); border:1px solid var(--color-rule,#ccc); }
</style>
