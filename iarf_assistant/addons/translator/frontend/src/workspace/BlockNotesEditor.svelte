<script>
  import { noteElementId } from '../lib/alignment-notes.js';
  import { renderNoteBody } from '../lib/note-body.js';
  import { tick } from "svelte";

  export let block;
  export let notes = [];
  export let accessRole = "owner";
  export let createRequest = null;
  export let busy = false;
  export let error = "";
  export let onCreate = async () => false;
  export let onRevise = async () => false;
  export let onTransition = async () => false;
  export let expanded = false;

  let editing = null;
  let bodyElement = null;
  let handledCreateKey = null;

  $: canEdit = accessRole === "owner" || accessRole === "reviewer";
  $: visibleNotes = notes.filter((note) => note.state !== "retired");
  $: retiredNotes = notes.filter((note) => note.state === "retired");
  $: needsReviewCount = visibleNotes.filter((note) => note.state === "needs_review").length;
  $: noteCountLabel = visibleNotes.length
    ? `${visibleNotes.length} note${visibleNotes.length === 1 ? "" : "s"}`
    : "No notes";
  $: blockNumber = Math.max(1, Number(block?.order) || 1);
  $: if (error) expanded = true;
  $: if (createRequest?.key && createRequest.key !== handledCreateKey) {
    handledCreateKey = createRequest.key;
    beginCreate(createRequest.anchor);
  }

  function emptyDraft() {
    return {
      id: "",
      expected_head_version: 0,
      state: "active",
      anchor_kind: "whole_block",
      anchor_text: "",
      occurrence_index: 0,
      body: "",
      citation_ids: []
    };
  }

  async function beginCreate(anchor = null) {
    const draft = emptyDraft();
    if (anchor && ["source", "target"].includes(anchor.kind) && typeof anchor.text === "string") {
      draft.anchor_kind = anchor.kind;
      draft.anchor_text = anchor.text;
      draft.occurrence_index = Math.max(0, Number(anchor.occurrence_index) || 0);
    }
    expanded = true;
    editing = draft;
    await tick();
    bodyElement?.focus();
  }

  function beginEdit(note) {
    expanded = true;
    editing = {
      id: note.id,
      expected_head_version: note.version,
      state: note.state,
      anchor_kind: note.anchor?.kind || "whole_block",
      anchor_text: note.anchor?.text || "",
      occurrence_index: Number(note.anchor?.occurrence_index || 0),
      body: note.body || "",
      citation_ids: Array.isArray(note.citation_ids) ? [...note.citation_ids] : []
    };
  }

  async function submit(event) {
    event.preventDefault();
    if (!canEdit || busy || !editing?.body.trim()) return;
    const wholeBlock = editing.anchor_kind === "whole_block";
    const targetAnchor = editing.anchor_kind === "target";
    const payload = {
      body: editing.body.trim(),
      anchor: {
        kind: wholeBlock ? "block" : editing.anchor_kind,
        anchor_text: wholeBlock ? null : editing.anchor_text,
        occurrence_index: wholeBlock ? null : Number(editing.occurrence_index || 0),
        target_version: targetAnchor ? Number(block?.target?.active_version || 0) : null
      },
      citation_ids: editing.citation_ids
    };
    const saved = editing.id
      ? await onRevise(editing.id, payload, editing.expected_head_version)
      : await onCreate(payload);
    if (saved) editing = null;
  }

  function setCitations(value) {
    editing = {
      ...editing,
      citation_ids: value.split(",").map((item) => item.trim()).filter(Boolean).slice(0, 32)
    };
  }
</script>

<details id={`block-notes-${block.id}`} class="notes-editor" aria-label={`Translator notes for block ${blockNumber}`} bind:open={expanded}>
  <summary hidden>Translator notes</summary>

  <div class="notes-content">
      <div class="notes-toolbar">
        <strong>Translator&rsquo;s notes <span>{noteCountLabel}</span></strong>
        {#if needsReviewCount}<span class="review-count">{needsReviewCount} to review</span>{/if}
        {#if canEdit}
        <button type="button" class="add-button" disabled={busy} on:click={() => beginCreate()}>Add note</button>
        {/if}
      </div>

    {#if error}<p class="note-error" role="alert">{error}</p>{/if}

    {#if editing && canEdit}
      <form class="note-form" on:submit={submit}>
        <div class="form-heading"><strong>{editing.id ? "Edit annotation" : "Add reader annotation"}</strong><button type="button" disabled={busy} on:click={() => (editing = null)}>Cancel</button></div>
        <label>
          Attach to
          <select bind:value={editing.anchor_kind} disabled={busy}>
            <option value="whole_block">The whole block</option>
            <option value="source">An exact source phrase</option>
            <option value="target" disabled={!block?.target}>An exact translated phrase</option>
          </select>
        </label>
        {#if editing.anchor_kind !== "whole_block"}
          <div class="anchor-grid">
            <label>
              Exact phrase
              <input bind:value={editing.anchor_text} maxlength="160" required disabled={busy} />
            </label>
            <label>
              Occurrence <small>0 is the first</small>
              <input type="number" min="0" bind:value={editing.occurrence_index} required disabled={busy} />
            </label>
          </div>
        {/if}
        <label>
          Reader-facing note
          <textarea bind:this={bodyElement} bind:value={editing.body} rows="4" maxlength="4000" required disabled={busy} aria-describedby={`note-format-help-${block.id}`}></textarea>
        </label>
        <small id={`note-format-help-${block.id}`}>Supports *italics*, **bold**, and `inline code`. Escape a formatting marker with a backslash to show it literally.</small>
        <label>
          Dossier reference IDs <small>Optional, comma separated</small>
          <input value={editing.citation_ids.join(", ")} maxlength="4200" disabled={busy} on:input={(event) => setCitations(event.currentTarget.value)} />
        </label>
        <button type="submit" class="save-button" disabled={busy || !editing.body.trim() || (editing.anchor_kind !== "whole_block" && !editing.anchor_text.trim())}>{busy ? "Saving…" : "Save annotation"}</button>
      </form>
    {/if}

    {#if visibleNotes.length}
      <ol class="note-list">
        {#each visibleNotes as note (note.id)}
          <li id={noteElementId(block.id, note.id)} class:needs-review={note.state === "needs_review"} class:proposal={note.state === "proposed"}>
            <div class="note-meta">
              <span>{note.state === "needs_review" ? "Anchor needs review" : note.state === "proposed" ? "Proposed note" : note.anchor?.kind === "whole_block" ? "Whole block" : `${note.anchor?.kind} phrase`}</span>
              <span>{note.author?.label || note.author?.type || "Translator"}</span>
            </div>
            {#if note.anchor?.text}<p class="note-anchor">{note.anchor.text}</p>{/if}
            <p class="note-body" data-note-body dir="auto" use:renderNoteBody={note}></p>
            {#if note.citation_ids?.length}<p class="citations">References: {note.citation_ids.join(", ")}</p>{/if}
            {#if canEdit}
              <div class="note-actions">
                {#if note.state === "proposed"}
                  <button type="button" disabled={busy} on:click={() => onTransition(note.id, "active", note.version)}>Approve</button>
                {/if}
                <button type="button" disabled={busy} on:click={() => beginEdit(note)}>{note.state === "needs_review" ? "Re-anchor" : "Edit"}</button>
                <button type="button" disabled={busy} on:click={() => onTransition(note.id, "retired", note.version)}>Retire</button>
              </div>
            {/if}
          </li>
        {/each}
      </ol>
    {:else if !editing}
      <p class="empty">No reader annotations for this block.</p>
    {/if}

    {#if canEdit && retiredNotes.length}
      <details class="retired-notes">
        <summary>{retiredNotes.length} retired note{retiredNotes.length === 1 ? "" : "s"}</summary>
        <ul>
          {#each retiredNotes as note (note.id)}
            <li><span use:renderNoteBody={note}></span><button type="button" disabled={busy} on:click={() => onTransition(note.id, "active", note.version)}>Restore</button></li>
          {/each}
        </ul>
      </details>
    {/if}
  </div>
</details>

<style>
  .notes-editor { color:var(--color-ink-soft,#4d4842); }
  .notes-editor[open] { margin:.25rem 1rem 1rem; border-top:1px solid color-mix(in srgb,var(--color-rule,#d0c8bc) 72%,transparent); }
  .notes-editor > summary { display:none; }
  .review-count { padding:.1rem .28rem; border:1px solid color-mix(in srgb,var(--color-warning,#9a6a18) 42%,transparent); color:var(--color-warning,#9a6a18); font-size:.59rem; font-weight:700; }
  .notes-content { display:grid; gap:.65rem; padding:.65rem 0 0; }
  .notes-toolbar { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; min-height:1.2rem; }
  .notes-toolbar strong { font-size:.72rem; font-weight:650; }
  .notes-toolbar strong span { margin-left:.4rem; color:var(--color-ink-muted,#675f56); font-weight:400; }
  .notes-toolbar .add-button { margin-left:auto; }
  .note-meta { color:var(--color-ink-muted,#675f56); font-size:.59rem; font-weight:760; letter-spacing:.1em; text-transform:uppercase; }
  .add-button, .note-actions button, .form-heading button, .retired-notes button { border:0; border-bottom:1px solid currentColor; padding:.16rem; background:transparent; color:var(--color-clay,#7c6053); cursor:pointer; font:inherit; font-size:.69rem; font-weight:700; }
  button:disabled { opacity:.52; cursor:not-allowed; }
  .note-error { margin:0; padding:.45rem .55rem; border:1px solid color-mix(in srgb,var(--color-danger,#b33) 35%,transparent); color:var(--color-danger,#b33); font-size:.75rem; }
  .note-form { display:grid; gap:.58rem; padding:.7rem; border:1px solid var(--color-rule,#d0c8bc); background:var(--color-surface,#faf8f3); }
  .form-heading { display:flex; justify-content:space-between; gap:1rem; }
  .form-heading strong { font-family:var(--font-editorial,Georgia,serif); font-size:.84rem; }
  label { display:grid; gap:.28rem; color:var(--color-ink-soft,#4d4842); font-size:.71rem; font-weight:700; }
  label small { color:var(--color-ink-muted,#675f56); font-weight:400; }
  input, select, textarea { width:100%; box-sizing:border-box; border:1px solid var(--color-rule-strong,#968b7d); padding:.48rem .54rem; background:var(--color-surface,#faf8f3); color:var(--color-ink,#302d29); font:inherit; font-size:.78rem; }
  textarea { resize:vertical; line-height:1.45; }
  .anchor-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(7rem,.25fr); gap:.55rem; }
  .save-button { justify-self:end; min-height:2rem; border:1px solid var(--color-umber,#5c5149); padding:.42rem .62rem; background:var(--color-umber,#5c5149); color:var(--color-surface,#faf8f3); cursor:pointer; font:inherit; font-size:.72rem; font-weight:700; }
  .note-list { display:grid; gap:.55rem; margin:0; padding:0; list-style:none; }
  .note-list > li { display:grid; gap:.3rem; padding:.62rem; border:1px solid var(--color-rule,#d0c8bc); background:color-mix(in srgb,var(--color-surface,#faf8f3) 58%,transparent); }
  .note-list > li.needs-review { border-color:var(--color-warning,#9a6a18); }
  .note-list > li.proposal { border-style:dashed; }
  .note-meta { display:flex; justify-content:space-between; gap:.7rem; }
  .note-anchor { width:fit-content; margin:0; padding:.1rem .28rem; background:var(--color-paper,#f1eee7); color:var(--color-ink,#302d29); font-size:.73rem; font-weight:650; }
  .note-body, .citations, .empty { margin:0; white-space:pre-wrap; font-size:.78rem; line-height:1.5; }
  .citations, .empty { color:var(--color-ink-muted,#675f56); font-size:.7rem; }
  .note-actions { display:flex; justify-content:flex-end; gap:.5rem; }
  .retired-notes summary { cursor:pointer; color:var(--color-ink-muted,#675f56); font-size:.7rem; }
  .retired-notes ul { display:grid; gap:.35rem; margin:.45rem 0 0; padding:0; list-style:none; }
  .retired-notes li { display:flex; justify-content:space-between; gap:.6rem; font-size:.72rem; }
  @media (max-width:620px) { .anchor-grid { grid-template-columns:1fr; } }
</style>
