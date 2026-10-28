<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher, onDestroy } from "svelte";
  import { cleanupDocument, undoDocumentCleanup, type CleanupResult } from "../lib/api";
  export let documentId: string;
  export let disabled = false;
  export let compact = false;
  const dispatch = createEventDispatcher<{ saved: void }>();
  let expanded = false;
  let headers = true;
  let footers = true;
  let pageInput = "";
  let busy = false;
  let error = "";
  let preview: CleanupResult | null = null;
  let saved: CleanupResult | null = null;
  let requestKey = "";
  let previewOptions: { roles: string[]; page_numbers?: number[] } | null = null;
  let undoKey = "";
  let generation = 0;
  let lastId = documentId;
  onDestroy(() => { generation += 1; });
  $: if (lastId !== documentId) {
    lastId = documentId; generation += 1; preview = null; saved = null;
    busy = false; error = ""; expanded = false;
  }
  function invalidate() { preview = null; previewOptions = null; }
  function options() {
    const roles = [...(headers ? ["header"] : []), ...(footers ? ["footer"] : [])];
    if (!roles.length) throw new Error("Select headers, footers, or both.");
    if (!pageInput.trim()) return { roles };
    const pages = new Set<number>();
    for (const part of pageInput.split(",")) {
      const match = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
      if (!match) throw new Error("Use physical PDF pages, for example 1-20, 24, 30-40.");
      const start = Number(match[1]); const end = Number(match[2] || match[1]);
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start || end - start > 9999)
        throw new Error("Invalid page range. Select at most 10,000 pages per cleanup.");
      for (let page = start; page <= end; page++) pages.add(page);
      if (pages.size > 10000) throw new Error("Select at most 10,000 pages per cleanup.");
    }
    return { roles, page_numbers: [...pages].sort((a, b) => a - b) };
  }
  async function inspect() {
    const id = documentId; const version = ++generation;
    busy = true; error = ""; invalidate();
    try {
      const selected = options();
      const result = await cleanupDocument(id, { ...selected, dry_run: true });
      if (documentId !== id || generation !== version) return;
      previewOptions = selected; preview = result; requestKey = crypto.randomUUID();
    } catch (err) { if (generation === version) error = err instanceof Error ? err.message : "Preview failed."; }
    finally { if (generation === version) busy = false; }
  }
  async function remove() {
    if (!preview || !previewOptions) return;
    const id = documentId; const version = ++generation;
    busy = true; error = "";
    try {
      const result = await cleanupDocument(id, {
        ...previewOptions, expected_revision: preview.revision, idempotency_key: requestKey, dry_run: false
      });
      if (documentId !== id || generation !== version) return;
      saved = result; undoKey = crypto.randomUUID(); invalidate(); dispatch("saved");
    } catch (err) { if (generation === version) error = err instanceof Error ? err.message : "Cleanup failed."; }
    finally { if (generation === version) busy = false; }
  }
  async function undo() {
    if (!saved?.change_id) return;
    const id = documentId; const version = ++generation;
    busy = true; error = "";
    try {
      await undoDocumentCleanup(id, saved.change_id, saved.revision, undoKey);
      if (documentId !== id || generation !== version) return;
      saved = null; invalidate(); dispatch("saved");
    } catch (err) { if (generation === version) error = err instanceof Error ? err.message : "Undo failed."; }
    finally { if (generation === version) busy = false; }
  }
</script>

<div class="cleanup" class:compact>
  <button
    class:button={!compact}
    class:dp-icon-btn={compact}
    type="button"
    disabled={disabled || busy}
    aria-expanded={expanded}
    aria-label="Remove page headers and footers"
    title="Remove page headers and footers"
    on:click={() => (expanded = !expanded)}
  >
    {#if compact}<Icon name="trash" size={16} />{:else}Remove headers / footers…{/if}
  </button>
  {#if expanded}
    <section class="cleanup-panel" aria-label="Remove page headers and footers">
      <p>Remove only elements already marked as page headers or footers. Ordinary headings and footnotes are not selected.</p>
      <fieldset disabled={disabled || busy}>
        <legend>What to remove</legend>
        <label><input type="checkbox" bind:checked={headers} on:change={invalidate} /> Headers</label>
        <label><input type="checkbox" bind:checked={footers} on:change={invalidate} /> Footers</label>
        <label class="pages">Physical pages <input type="text" placeholder="All pages, or 1-20, 24" bind:value={pageInput} on:input={invalidate} /></label>
      </fieldset>
      <p class="dp-muted">Existing exports must be regenerated. Existing translations retain their original source snapshot.</p>
      {#if error}<p class="dp-notice error" role="alert">{error}</p>{/if}
      {#if saved}<p role="status">Cleanup saved. {#if saved.change_id}<button class="button ghost" type="button" disabled={busy || disabled} on:click={undo}>Undo this cleanup</button>{/if}</p>{/if}
      {#if preview?.cleanup}
        <div class="cleanup-preview" aria-live="polite">
          <strong>{preview.cleanup.removed_count} marked elements to remove.</strong>
          {#if preview.cleanup.skipped_count}<p>{preview.cleanup.skipped_count} protected elements will be kept.</p>{/if}
          <ul>{#each preview.cleanup.examples as item}<li><strong>{item.role}{item.page_numbers.length ? `, page ${item.page_numbers.join(", ")}` : ""}:</strong> {item.text}</li>{/each}</ul>
          {#if preview.cleanup.examples_truncated}<p class="dp-muted">Showing a sample, not every match.</p>{/if}
        </div>
      {/if}
      <div class="cleanup-actions">
        <button class="button" type="button" disabled={disabled || busy || (!headers && !footers)} on:click={inspect}>{busy ? "Working…" : "Preview removal"}</button>
        {#if preview?.cleanup?.removed_count}<button class="button danger" type="button" disabled={disabled || busy} on:click={remove}>Confirm removal of {preview.cleanup.removed_count} elements</button>{/if}
      </div>
    </section>
  {/if}
</div>
<style>
  .cleanup { flex-basis: 100%; }
  .cleanup.compact { display: contents; }
  .cleanup.compact .cleanup-panel { flex: 1 0 100%; order: 100; max-width: none; }
  .cleanup-panel { margin-top: .75rem; padding: 1rem; border: 1px solid var(--border-color, #cbd5e1); border-radius: .5rem; max-width: 60rem; }
  p { margin: .4rem 0 .8rem; }
  fieldset { display: flex; flex-wrap: wrap; align-items: center; gap: 1rem; border: 0; padding: 0; margin: 0 0 .75rem; }
  legend { font-weight: 600; margin-bottom: .5rem; }
  label { display: flex; align-items: center; gap: .4rem; }
  .pages { flex-wrap: wrap; }
  input[type="text"] { min-width: 14rem; max-width: 100%; }
  .cleanup-preview { max-height: 14rem; overflow: auto; margin: .75rem 0; overflow-wrap: anywhere; }
  ul { padding-left: 1.25rem; }
  li { margin: .35rem 0; }
  .cleanup-actions { display: flex; flex-wrap: wrap; gap: .75rem; }
</style>
