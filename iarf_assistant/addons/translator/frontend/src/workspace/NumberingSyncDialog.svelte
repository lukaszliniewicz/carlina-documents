<script>
  import { onMount, onDestroy } from "svelte";
  import { Dialog } from "@carlina/ui";
  import { createIdempotencyKey, isAbortError } from "../lib/api.js";
  import { preparePreviewDocument } from "../lib/preview-document.js";
  import SemanticPreviewDialog from "./SemanticPreviewDialog.svelte";

  export let client;
  export let onClose = () => {};
  export let onApplied = () => {};

  const controller = new AbortController();
  let comparison = null;
  let selected = [];
  let busy = false;
  let error = "";
  let notice = "";
  let pending = null;
  let previewOpen = false;
  let previewLoading = false;
  let previewHtml = "";
  let previewError = "";

  async function refresh() {
    if (busy) return;
    busy = true;
    error = "";
    try {
      const next = await client.numberingPreview({ signal: controller.signal });
      if (controller.signal.aborted) return;
      comparison = next;
      selected = [];
      pending = null;
    } catch (caught) {
      if (!isAbortError(caught)) error = caught.message || "Unable to compare list numbering.";
    } finally { busy = false; }
  }

  async function submit(action) {
    if (busy || !comparison) return;
    const payload = { preview_token: comparison.preview_token };
    if (action === "apply") payload.selected_ids = [...selected].sort();
    const intent = JSON.stringify({ action, ...payload });
    if (pending?.intent !== intent) pending = { intent, key: createIdempotencyKey("numbering") };
    busy = true;
    error = "";
    notice = "";
    try {
      await client[action === "apply" ? "numberingApply" : "numberingUndo"](
        { ...payload, idempotency_key: pending.key }, { signal: controller.signal }
      );
      if (controller.signal.aborted) return;
      pending = null;
      notice = action === "apply" ? "Numbering synchronized. Generate new exports to include these corrections." : "The previous numbering has been restored. Generate new exports to include it.";
      comparison = null;
      selected = [];
      await onApplied();
    } catch (caught) {
      if (!isAbortError(caught)) error = caught.message || "Numbering could not be changed. Retry or refresh the comparison.";
    } finally { busy = false; }
    if (!error) await refresh();
  }

  async function previewSource() {
    previewOpen = true;
    previewLoading = true;
    previewError = "";
    try {
      const html = await client.numberingSourcePreview({ signal: controller.signal });
      const prepared = await preparePreviewDocument(html);
      if (!controller.signal.aborted) previewHtml = prepared;
    } catch (caught) {
      if (!isAbortError(caught)) previewError = caught.message || "Unable to preview source numbering.";
    } finally { previewLoading = false; }
  }

  function toggle(id) {
    selected = selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id];
  }

  function attributeName(name) {
    return { start: "Starting number", type: "Number style", reversed: "Reverse numbering", value: "Item number" }[name] || name;
  }

  function attributeValue(name, value) {
    if (name === "reversed") return value === null ? "Off" : "On";
    if (value === null) return "Automatic";
    if (name === "type") return { "1": "1, 2, 3", a: "a, b, c", A: "A, B, C", i: "i, ii, iii", I: "I, II, III" }[value] || value;
    return value;
  }

  onMount(refresh);
  onDestroy(() => controller.abort());
</script>

<Dialog open={true} title="Synchronize list numbering" description="Bring numbering corrections from the extracted document into this translation. Choose the lists to update; translated wording stays intact." onclose={() => !busy && onClose()} size="lg">
  <div class="numbering-panel" aria-busy={busy}>
    {#if error}<p class="message error" role="alert">{error}</p>{/if}
    {#if notice}<p class="message" role="status">{notice}</p>{/if}
    <div class="actions">
      <button type="button" disabled={busy} on:click={refresh}>{busy ? "Working…" : "Refresh comparison"}</button>
      <button type="button" disabled={busy || previewLoading} on:click={previewSource}>Preview current source numbering</button>
    </div>
    <p class="hint">The source preview shows the numbering currently used by this translation. After applying changes, check the translation preview too. Previously downloaded files stay unchanged.</p>
    {#if comparison}
      {#if comparison.changes?.length}
        <div class="selection-heading">
          <h3>Available corrections ({comparison.changes.length})</h3>
          <button type="button" disabled={busy} on:click={() => selected = selected.length === comparison.changes.length ? [] : comparison.changes.map((change) => change.id)}>{selected.length === comparison.changes.length ? "Clear selection" : "Select all"}</button>
        </div>
        <div class="list-changes">
          {#each comparison.changes as change (change.id)}
            <label class="list-change">
              <input type="checkbox" checked={selected.includes(change.id)} disabled={busy} on:change={() => toggle(change.id)} />
              <span class="change-content">
                <strong>List {change.root_index + 1}</strong>
                <span class="excerpt">{change.excerpt}</span>
                {#each change.changes as node}
                  {#each Object.keys(node.after) as name}
                    <span class="attribute-change">{attributeName(name)}{node.node_index ? ` · element ${node.node_index + 1}` : ""}: <span>{attributeValue(name, node.before[name])} → {attributeValue(name, node.after[name])}</span></span>
                  {/each}
                {/each}
              </span>
            </label>
          {/each}
        </div>
      {:else}
        <p class="message">No matching lists have new numbering corrections.</p>
      {/if}
      {#if comparison.conflicts?.length}
        <section class="conflicts" aria-labelledby="numbering-conflicts-title">
          <h3 id="numbering-conflicts-title">Lists needing manual review ({comparison.conflicts.length})</h3>
          <p>These lists could not be matched safely. Check changes to their text, structure, or repeated content before synchronizing.</p>
          <ul>{#each comparison.conflicts as conflict}<li><strong>List {conflict.root_index + 1}</strong> — {conflict.excerpt}</li>{/each}</ul>
        </section>
      {/if}
      <div class="actions footer">
        <button type="button" disabled={busy || !comparison.can_undo} on:click={() => submit("undo")}>Undo latest synchronization</button>
        <button type="button" class="primary" disabled={busy || !selected.length} on:click={() => submit("apply")}>Apply selected ({selected.length})</button>
      </div>
    {/if}
  </div>
</Dialog>
<SemanticPreviewDialog {previewOpen} {previewLoading} {previewHtml} {previewError} onClosePreview={() => (previewOpen = false)} />

<style>
  .numbering-panel { display:grid; gap:1rem; }
  p, h3 { margin:0; }
  h3 { font-size:1rem; }
  .actions, .selection-heading { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:.6rem; }
  button { border:1px solid var(--color-rule-strong); padding:.5rem .7rem; background:var(--color-surface); color:var(--color-ink); font:inherit; font-size:.8rem; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .primary { background:var(--color-clay); color:var(--color-surface); border-color:var(--color-clay); }
  .hint, .conflicts p { color:var(--color-ink-muted); font-size:.8rem; line-height:1.5; }
  .list-changes { display:grid; max-height:45vh; overflow:auto; border:1px solid var(--color-rule); }
  .list-change { display:flex; align-items:flex-start; gap:.7rem; padding:.8rem; border-bottom:1px solid var(--color-rule); cursor:pointer; }
  input { margin-top:.2rem; flex-shrink:0; }
  .change-content { display:grid; gap:.3rem; min-width:0; font-size:.85rem; }
  .excerpt { overflow-wrap:anywhere; color:var(--color-ink-muted); }
  .attribute-change { font-size:.78rem; }
  .attribute-change > span { font-weight:650; }
  .message { padding:.75rem; background:var(--color-surface-muted); border:1px solid var(--color-rule); font-size:.85rem; }
  .error { color:var(--color-danger); }
  .conflicts { display:grid; gap:.5rem; border-left:3px solid var(--color-clay); padding:.75rem; }
  ul { margin:0; padding-left:1.3rem; font-size:.8rem; }
  li + li { margin-top:.5rem; }
  .footer { padding-top:.75rem; border-top:1px solid var(--color-rule); }
</style>
