<script lang="ts">
  import { onDestroy, onMount, createEventDispatcher } from "svelte";
  import { listIssues, reportIssue, updateIssue, type ExtractionIssue, type IssueKind, type IssuePage, type IssueStatus } from "../lib/issues";
  export let documentId: string;
  export let pdfUrl = "";
  const dispatch = createEventDispatcher<{ preview: void }>();
  const labels: Record<IssueStatus, string> = { open: "Open", confirmed: "Confirmed problem", resolved: "Resolved", dismissed: "Dismissed" };
  const kinds: Record<IssueKind, string> = { ambiguous_text: "Unclear reading", name_check: "Name spelling", structure: "Document structure", diagram: "Diagram", source_damage: "Source damage", other: "Other" };
  let data: IssuePage | null = null;
  let selected: ExtractionIssue | null = null;
  let filter = "";
  let offset = 0;
  let loading = false;
  let saving = false;
  let error = "";
  let note = "";
  let nextStatus: IssueStatus = "confirmed";
  let adding = false;
  let page: number | undefined;
  let kind: IssueKind = "ambiguous_text";
  let excerpt = "";
  let reason = "";
  let controller: AbortController | null = null;
  let alive = true;
  // Preserve an uncertain delivery's key while the request contents are unchanged.
  let intent = "";
  let requestKey = "";
  function keyFor(value: unknown) {
    const serialized = JSON.stringify(value);
    if (intent !== serialized) { intent = serialized; requestKey = crypto.randomUUID(); }
    return requestKey;
  }
  onMount(() => { void load(); });
  onDestroy(() => { alive = false; controller?.abort(); });
  async function load(selectId?: string) {
    controller?.abort();
    const current = new AbortController();
    controller = current;
    loading = true;
    error = "";
    try {
      const result = await listIssues(documentId, filter, offset, current.signal);
      if (!alive || current.signal.aborted) return;
      data = result;
      selected = result.items.find(item => item.id === selectId) || null;
      if (selected) choose(selected);
    } catch (err) {
      if (alive && !current.signal.aborted) error = err instanceof Error ? err.message : "Could not load review issues.";
    } finally { if (alive && controller === current) loading = false; }
  }
  function choose(issue: ExtractionIssue) {
    selected = issue; adding = false; note = "";
    nextStatus = issue.status === "open" ? "confirmed" : issue.status === "confirmed" ? "resolved" : "open";
  }
  async function saveReview() {
    if (!selected || !note.trim()) return;
    saving = true; error = "";
    const issue = selected;
    try {
      const result = await updateIssue(documentId, issue, nextStatus, note.trim(), keyFor([issue.id, issue.version, issue.current_revision, nextStatus, note.trim()]));
      if (!alive) return;
      note = ""; intent = "";
      await load(result.id);
    } catch (err) { if (alive) error = err instanceof Error ? err.message : "Could not save review. Reload if the document or issue changed."; }
    finally { if (alive) saving = false; }
  }
  async function addIssue() {
    if (!data || !reason.trim()) return;
    saving = true; error = "";
    const payload = { kind, excerpt, reason: reason.trim(), suggested_readings: [], references: [] };
    const key = keyFor([data.current_revision, page ?? null, payload]);
    try {
      const result = await reportIssue(documentId, data.current_revision, page ?? null, { key: `manual-${key}`, ...payload }, key);
      if (!alive) return;
      adding = false; reason = ""; excerpt = ""; page = undefined; intent = "";
      filter = ""; offset = 0;
      await load(result.id);
    } catch (err) { if (alive) error = err instanceof Error ? err.message : "Could not add issue."; }
    finally { if (alive) saving = false; }
  }
  function safeUrl(url: string): string | undefined {
    try { const parsed = new URL(url); return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : undefined; } catch { return undefined; }
  }
</script>

<section class="issues" aria-label="Extraction review issues">
  <header class="intro"><div><h3>Review issues</h3><p>Unclear readings and possible problems found during extraction or review. Check the source before making corrections.</p></div><button class="button ghost" type="button" on:click={() => dispatch("preview")}>Back to document</button></header>
  <div class="toolbar">
    <label>Status <select bind:value={filter} disabled={saving} on:change={() => { offset = 0; selected = null; void load(); }}><option value="">All statuses</option>{#each Object.entries(labels) as [value, label]}<option {value}>{label}</option>{/each}</select></label>
    <button class="button ghost" type="button" disabled={loading || saving} on:click={() => load(selected?.id)}>Refresh</button>
    <button class="button" type="button" disabled={!data || saving} on:click={() => { adding = true; selected = null; }}>Add issue</button>
    {#if data}<span class="counts">{data.counts.open || 0} open · {data.counts.confirmed || 0} confirmed</span>{/if}
  </div>
  {#if error}<p class="dp-notice error" role="alert">{error}</p>{/if}
  {#if loading}<p role="status">Loading issues…</p>{/if}
  <div class="columns">
    <aside aria-label="Issue list">
      {#if data && !data.items.length && !loading}<p class="empty">No issues match this view. An empty ledger does not establish that the extraction is error-free.</p>{/if}
      {#each data?.items || [] as issue (issue.id)}
        <button class="issue-row" class:active={selected?.id === issue.id} type="button" disabled={saving} aria-pressed={selected?.id === issue.id} on:click={() => choose(issue)}>
          <span class="row-top"><strong>{issue.page_number ? `Page ${issue.page_number}` : "Document"}</strong><span>{labels[issue.status]}</span></span>
          <span>{kinds[issue.report.kind]}</span><span class="excerpt">{issue.report.excerpt || issue.report.reason}</span>
          {#if issue.context_stale}<small>Document changed since report</small>{/if}
        </button>
      {/each}
      {#if data && data.total > 25}<nav class="pagination" aria-label="Issue pages"><button class="button ghost" disabled={!offset || loading || saving} on:click={() => { offset -= 25; void load(); }}>Previous</button><span>{offset + 1}–{Math.min(offset + 25, data.total)} of {data.total}</span><button class="button ghost" disabled={offset + 25 >= data.total || loading || saving} on:click={() => { offset += 25; void load(); }}>Next</button></nav>{/if}
    </aside>
    <div class="detail">
      {#if adding}
        <form on:submit|preventDefault={addIssue}>
          <h4>Add a review issue</h4>
          <label>Physical PDF page (optional)<input type="number" min="1" step="1" bind:value={page} /></label>
          <label>Kind<select bind:value={kind}>{#each Object.entries(kinds) as [value, label]}<option {value}>{label}</option>{/each}</select></label>
          <label>Reading in the extraction<textarea bind:value={excerpt} maxlength="2000" rows="3"></textarea></label>
          <label>What needs checking?<textarea bind:value={reason} required maxlength="2000" rows="3"></textarea></label>
          <button class="button" disabled={saving || !reason.trim()} type="submit">{saving ? "Saving…" : "Save issue"}</button>
        </form>
      {:else if selected}
        <h4>{kinds[selected.report.kind]} · {selected.page_number ? `page ${selected.page_number}` : "document"}</h4>
        {#if selected.context_stale}<p class="dp-notice warning">The document changed after this report. Compare its original reading with the current document before resolving it.</p>{/if}
        {#if selected.report.excerpt}<blockquote>{selected.report.excerpt}</blockquote>{/if}
        <p class="preserve">{selected.report.reason}</p>
        {#if selected.report.suggested_readings.length}<h5>Candidate readings</h5><ul>{#each selected.report.suggested_readings as candidate}<li>{candidate}</li>{/each}</ul>{/if}
        {#if selected.report.references.length}<h5>Corroborating references</h5><ul>{#each selected.report.references as reference}<li><a href={safeUrl(reference.url)} target="_blank" rel="noopener noreferrer">{reference.title || reference.url}</a><p>{reference.note}</p></li>{/each}</ul>{/if}
        {#if pdfUrl && selected.page_number}<a class="source-link" href={`${pdfUrl}#page=${selected.page_number}`} target="_blank" rel="noopener noreferrer">Open source PDF at page {selected.page_number}</a>{/if}
        <form on:submit|preventDefault={saveReview}>
          <h5>Review decision</h5><p class="muted">This saves a review note. Correct the document separately, then resolve the issue after checking the change.</p>
          <label>Status<select bind:value={nextStatus}>{#each Object.entries(labels) as [value, label]}<option {value}>{label}</option>{/each}</select></label>
          <label>Review note<textarea bind:value={note} required maxlength="2000" rows="3" placeholder="What did you verify or correct?"></textarea></label>
          <button class="button" type="submit" disabled={saving || !note.trim()}>{saving ? "Saving…" : "Save review"}</button>
        </form>
        {#if selected.history.length}<details><summary>Review history ({selected.history.length})</summary>{#each selected.history as entry}<p><strong>{labels[entry.status]}</strong> — {entry.note}{#if entry.timestamp}<br /><small><time datetime={entry.timestamp}>{new Date(entry.timestamp).toLocaleString()}</time></small>{/if}</p>{/each}</details>{/if}
      {:else}<p class="empty">Select an issue to inspect the reading, evidence, and review history.</p>{/if}
    </div>
  </div>
</section>

<style>
  .issues { padding: 1.25rem; overflow: auto; height: 100%; min-height: 0; box-sizing: border-box; }
  h3,h4,h5 { margin: 0 0 .65rem; } h5 { margin-top: 1.2rem; }
  .intro,.toolbar,.row-top,.pagination { display: flex; gap: .75rem; align-items: center; justify-content: space-between; }
  .intro { align-items: start; } .intro p,.muted,.counts,.empty { color: var(--text-secondary, #64748b); }
  .toolbar { justify-content: start; flex-wrap: wrap; margin: 1rem 0; }
  label { display: grid; gap: .35rem; } input,select,textarea { padding: .55rem; border: 1px solid var(--border-color, #cbd5e1); border-radius: .4rem; background: var(--bg-primary, white); color: inherit; max-width: 100%; }
  textarea { width: 100%; box-sizing: border-box; resize: vertical; }
  .columns { display: grid; grid-template-columns: minmax(230px, 1fr) minmax(300px, 2fr); gap: 1.5rem; }
  .detail { min-width: 0; overflow-wrap: anywhere; } .issue-row { width: 100%; display: grid; text-align: left; gap: .4rem; padding: .9rem; margin-bottom: .5rem; border: 1px solid var(--border-color, #cbd5e1); border-radius: .5rem; background: var(--bg-primary, white); color: inherit; cursor: pointer; }
  .issue-row.active { border-color: var(--accent-color, #3b82f6); box-shadow: inset 3px 0 var(--accent-color, #3b82f6); }
  .row-top { font-size: .85rem; flex-wrap: wrap; } .excerpt { overflow: hidden; display: -webkit-box; line-clamp: 2; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow-wrap: anywhere; }
  blockquote { margin: 1rem 0; padding: .75rem 1rem; border-left: 3px solid var(--border-color, #cbd5e1); background: var(--bg-secondary, #f1f5f9); white-space: pre-wrap; }
  .preserve { white-space: pre-wrap; } form { display: grid; gap: .85rem; margin: 1rem 0; } form .button { justify-self: start; }
  .source-link { display: inline-block; margin: .5rem 0; } details { margin-top: 1.25rem; } .pagination { font-size: .85rem; }
  @media (max-width: 700px) { .columns { grid-template-columns: 1fr; } .intro { flex-direction: column; } .issues { padding: .8rem; } }
</style>
