<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher, onDestroy } from "svelte";
  import {
    repairDocumentBoundaries, undoDocumentCleanup,
    type BoundaryAction, type BoundaryCandidate, type BoundaryRepairResult
  } from "../lib/api";
  export let documentId: string;
  export let disabled = false;
  export let compact = false;
  const dispatch = createEventDispatcher<{ saved: void }>();
  let expanded = false;
  let busy = false;
  let error = "";
  let preview: BoundaryRepairResult | null = null;
  let saved: BoundaryRepairResult | null = null;
  let selections: Record<string, BoundaryAction | ""> = {};
  let requestKey = "";
  let undoKey = "";
  let generation = 0;
  let lastId = documentId;
  $: decisions = Object.entries(selections).filter(([, action]) => action).map(([id, action]) => ({ id, action: action as BoundaryAction }));
  $: if (lastId !== documentId) {
    lastId = documentId; generation += 1; preview = null; saved = null;
    selections = {}; busy = false; error = ""; expanded = false;
  }
  onDestroy(() => { generation += 1; });

  async function inspect(offset = 0) {
    const id = documentId; const version = ++generation;
    const revision = offset ? preview?.revision : undefined;
    busy = true; error = ""; preview = null; selections = {};
    try {
      const result = await repairDocumentBoundaries(id, {
        offset, limit: 50, ...(revision ? { expected_revision: revision } : {}), dry_run: true
      });
      if (generation !== version || id !== documentId) return;
      preview = result; requestKey = crypto.randomUUID();
    } catch (err) { if (generation === version) error = err instanceof Error ? err.message : "Boundary scan failed."; }
    finally { if (generation === version) busy = false; }
  }
  function choose(id: string, action: string) {
    selections = { ...selections, [id]: action as BoundaryAction | "" };
    requestKey = crypto.randomUUID();
  }
  function joined(candidate: BoundaryCandidate, action: BoundaryAction | "" | undefined): string {
    if (!action) return "Leave these paragraphs unchanged.";
    if (action === "dehyphenate") return candidate.left.slice(0, -1) + candidate.right;
    if (action === "keep_hyphen") return candidate.left + candidate.right;
    return candidate.left + " " + candidate.right;
  }
  async function save() {
    if (!preview || !decisions.length) return;
    const id = documentId; const version = ++generation;
    busy = true; error = "";
    try {
      const result = await repairDocumentBoundaries(id, {
        decisions, expected_revision: preview.revision, idempotency_key: requestKey, dry_run: false
      });
      if (generation !== version || id !== documentId) return;
      saved = result; undoKey = crypto.randomUUID(); preview = null; selections = {};
      dispatch("saved");
    } catch (err) { if (generation === version) error = err instanceof Error ? err.message : "Boundary repair failed."; }
    finally { if (generation === version) busy = false; }
  }
  async function undo() {
    if (!saved?.change_id) return;
    const id = documentId; const version = ++generation;
    busy = true; error = "";
    try {
      await undoDocumentCleanup(id, saved.change_id, saved.revision, undoKey);
      if (generation !== version || id !== documentId) return;
      saved = null; preview = null; selections = {}; dispatch("saved");
    } catch (err) { if (generation === version) error = err instanceof Error ? err.message : "Undo failed."; }
    finally { if (generation === version) busy = false; }
  }
</script>

<div class="boundary-repair" class:compact>
  <button
    class:button={!compact}
    class:dp-icon-btn={compact}
    type="button"
    disabled={disabled || busy}
    aria-expanded={expanded}
    aria-label="Repair page continuations"
    title="Repair page continuations"
    on:click={() => (expanded = !expanded)}
  >
    {#if compact}<Icon name="fork" size={16} />{:else}Repair page continuations…{/if}
  </button>
  {#if expanded}
    <section class="repair-panel" aria-label="Repair page continuations">
      <p>Review words and sentences split by a physical page break. Nothing is selected automatically: a printed hyphen may be intentional.</p>
      <p class="dp-muted">Page markers and references are kept. Headings, notes and other protected structures are not crossed. Remove marked headers and footers first when they separate the paragraphs. Existing translations keep their original snapshots.</p>
      {#if error}<p class="dp-notice error" role="alert">{error}</p>{/if}
      {#if saved}<p role="status">Page repairs saved as one undoable change. {#if saved.change_id}<button class="button ghost" type="button" disabled={busy || disabled} on:click={undo}>Undo this repair</button>{/if}</p>{/if}
      <button class="button" type="button" disabled={busy || disabled} on:click={() => inspect()}>{busy ? "Working…" : "Scan page continuations"}</button>
      {#if preview?.boundary_repair}
        {@const report = preview.boundary_repair}
        <p role="status">{report.candidate_count} possible continuations found. {decisions.length} selected.</p>
        <fieldset class="candidates" disabled={busy || disabled}>
          <legend>Review each proposed join</legend>
          {#each report.candidates as candidate (candidate.id)}
            <article>
              <strong>Physical page {candidate.page_number}</strong>
              <div class="before"><span>{candidate.left}</span><span class="page-label">Page break</span><span>{candidate.right}</span></div>
              <label>Action for page {candidate.page_number}
                <select value={selections[candidate.id] || ""} on:change={(event) => choose(candidate.id, event.currentTarget.value)}>
                  <option value="">Leave unchanged</option>
                  {#if candidate.suggested_action === "dehyphenate"}
                    <option value="dehyphenate">Join word and remove hyphen</option>
                    <option value="keep_hyphen">Join word and keep hyphen</option>
                  {/if}
                  <option value="join_with_space">Join paragraphs with a space</option>
                </select>
              </label>
              <p class="after">{joined(candidate, selections[candidate.id])}</p>
            </article>
          {/each}
        </fieldset>
        <div class="actions">
          {#if decisions.length}<button class="button primary" type="button" disabled={busy || disabled} on:click={save}>Confirm {decisions.length} selected repairs</button>{/if}
          {#if report.offset > 0}<button class="button" type="button" disabled={busy || disabled || decisions.length > 0} on:click={() => inspect(Math.max(0, report.offset - report.limit))}>Previous candidates</button>{/if}
          {#if report.has_more}<button class="button" type="button" disabled={busy || disabled || decisions.length > 0} on:click={() => inspect(report.offset + report.limit)}>Next candidates</button>{/if}
        </div>
        {#if report.has_more && decisions.length}<p class="dp-muted">Save or clear the current selections before reviewing another page of candidates.</p>{/if}
      {/if}
    </section>
  {/if}
</div>
<style>
  .boundary-repair { flex-basis: 100%; }
  .boundary-repair.compact { display: contents; }
  .boundary-repair.compact .repair-panel { flex: 1 0 100%; order: 100; max-width: none; }
  .repair-panel { margin-top: .75rem; padding: 1rem; border: 1px solid var(--border-color, #cbd5e1); border-radius: .5rem; max-width: 60rem; }
  p { margin: .5rem 0 .8rem; }
  .candidates { border: 0; margin: .75rem 0; padding: 0; max-height: 28rem; overflow-y: auto; }
  legend { font-weight: 600; margin-bottom: .5rem; }
  article { border-top: 1px solid var(--border-color, #cbd5e1); padding: .85rem .25rem; overflow-wrap: anywhere; }
  .before { display: grid; gap: .3rem; margin: .5rem 0; }
  .page-label { font-size: .8rem; font-style: italic; opacity: .7; }
  label { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; }
  select { max-width: 100%; }
  .after { font-weight: 500; }
  .actions { display: flex; flex-wrap: wrap; gap: .75rem; }
</style>
