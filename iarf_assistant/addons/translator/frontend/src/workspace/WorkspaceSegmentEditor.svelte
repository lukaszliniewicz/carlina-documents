<!-- Glossary/source note renderers are escaped before reaching this component. -->
<!-- eslint-disable svelte/no-at-html-tags -->
<script>
  import { isApprovedStatus } from "../lib/utils.js";
  import { renderNoteBody } from "../lib/note-body.js";

  export let blocks = [];
  export let drafts = {};
  export let guestMode = false;
  export let translatingIds = new Set();
  export let autoExpand = () => {};
  export let renderContextNote = (value) => value;
  export let onDraftInput = () => {};
  export let onDraftFocus = () => {};
  export let onDraftBlur = () => {};
  export let onMerge = () => {};
  export let onSplit = () => {};
  export let onTranslate = () => {};
  export let onStatus = () => {};
  export let onResolveConflict = () => {};
  export let onRebaseConflict = () => {};
</script>

{#each blocks as block (block.key)}
  <article class="segment-block">
    <header class="segment-block-header">{block.label}</header>
    {#each block.segments as segment (segment.id)}
      <div class={`segment-row status-${segment.status}`}>
        <div class="source-col"><div class="source-content">{@html segment.highlighted_source_text}</div></div> <!-- eslint-disable-line svelte/no-at-html-tags -->
        <div class="target-col">
          <textarea
            class="target-textarea"
            value={drafts[segment.id]?.value ?? drafts[segment.id] ?? ""}
            readonly={guestMode}
            use:autoExpand
            on:focus={() => !guestMode && onDraftFocus(segment.id)}
            on:blur={() => !guestMode && onDraftBlur(segment.id)}
            on:input={(event) => !guestMode && onDraftInput(segment.id, event.currentTarget.value)}
            aria-label={`Translation for segment ${segment.id}`}
          ></textarea>
          {#if !guestMode}
          <div class="segment-actions">
            <button class="seg-btn" type="button" title="Merge with next segment" on:click={() => onMerge(segment.id)}>Merge</button>
            <button class="seg-btn" type="button" title="Split segment" on:click={() => onSplit(segment.id)}>Split</button>
            <button class="seg-btn primary" type="button" title="Translate with AI" disabled={guestMode || translatingIds.has(segment.id)} on:click={() => onTranslate(segment.id)}>
              {translatingIds.has(segment.id) ? "Translating..." : "Translate"}
            </button>
            <button class={`seg-btn ${isApprovedStatus(segment.status) ? "approved" : ""}`} type="button" on:click={() => onStatus(segment.id, "human_approved")}>Approve</button>
            <button class={`seg-btn ${segment.status === "needs_review" ? "needs-review" : ""}`} type="button" on:click={() => onStatus(segment.id, "needs_review")}>Review</button>
          </div>
          {/if}
          {#if drafts[segment.id]?.conflict}
            <div class="draft-conflict" role="alert">
              <strong>Server changed this segment.</strong>
              <span>Your draft is still here.</span>
              <button class="seg-btn" type="button" on:click={() => onResolveConflict(segment.id)}>Load server version</button>
              <button class="seg-btn primary" type="button" on:click={() => onRebaseConflict(segment.id)}>Keep local draft</button>
            </div>
          {/if}
          {#if segment.context_note}
            {#if segment.status === "error"}<p class="error-note">{segment.context_note}</p>{:else}<p class="note">{@html renderContextNote(segment.context_note)}</p>{/if} <!-- eslint-disable-line svelte/no-at-html-tags -->
          {/if}
          {#if segment.translator_notes?.length}
            <aside class="translator-notes" aria-label="Translator's notes">
              {#each segment.translator_notes as translatorNote (translatorNote.id)}
                <p><strong>Translator&rsquo;s note{translatorNote.anchor_text ? ` — ${translatorNote.anchor_text}` : ""}:</strong> <span use:renderNoteBody={translatorNote}></span></p>
              {/each}
            </aside>
          {/if}
        </div>
      </div>
    {/each}
  </article>
{/each}

<style>
  .segment-block { border:1px solid var(--color-rule); background:var(--color-surface); border-radius:.6rem; overflow:hidden; flex-shrink:0; margin-bottom:.8rem; }
  .segment-block-header { padding:.5rem .78rem; border-bottom:1px solid var(--color-rule); background:var(--color-paper-muted); text-transform:uppercase; font-size:.72rem; font-weight:700; letter-spacing:.08em; color:var(--color-ink-muted); }
  .segment-row { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); border-bottom:1px solid var(--color-rule); align-items:stretch; min-height:146px; }
  .segment-row:last-child { border-bottom:none; }
  .segment-row.status-human_approved,.segment-row.status-approved { background:color-mix(in srgb,var(--color-active-muted) 75%,var(--color-surface)); }
  .segment-row.status-needs_review { background:color-mix(in srgb,var(--color-warning-muted) 75%,var(--color-surface)); }
  .segment-row.status-error { background:color-mix(in srgb,var(--color-danger-muted) 75%,var(--color-surface)); }
  .source-col { border-right:1px solid var(--color-rule); padding:.8rem; line-height:1.55; min-height:144px; }
  .target-col { padding:.8rem; display:flex; flex-direction:column; gap:.45rem; min-height:144px; }
  .source-content { white-space:pre-wrap; word-break:break-word; unicode-bidi:plaintext; }
  .source-content :global(.glossary-highlight) { background:color-mix(in srgb,var(--color-warning-muted) 82%,var(--color-surface)); border-bottom:1px dashed var(--color-warning); padding:0 .12rem; border-radius:.22rem; cursor:pointer; transition:background-color 120ms ease; }
  .source-content :global(.glossary-highlight:hover) { background:color-mix(in srgb,var(--color-warning-muted) 58%,var(--color-surface)); }
  .source-content :global(.glossary-highlight:focus-visible) { outline:2px solid color-mix(in srgb,var(--color-active) 30%,transparent); outline-offset:2px; }
  .target-textarea { width:100%; min-height:84px; overflow-y:hidden; resize:none; box-sizing:border-box; border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); padding:.56rem .62rem; font:inherit; border-radius:.5rem; }
  .target-textarea:focus { outline:2px solid color-mix(in srgb,var(--color-active) 20%,transparent); border-color:color-mix(in srgb,var(--color-active) 38%,var(--color-rule)); outline-offset:1px; }
  .segment-actions { display:flex; flex-wrap:wrap; gap:.4rem; }
  .seg-btn { border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink-soft); padding:.34rem .58rem; cursor:pointer; font-size:.81rem; border-radius:.45rem; }
  .seg-btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .seg-btn.danger { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); }
  .seg-btn.approved { border-color:color-mix(in srgb,var(--color-active) 35%,var(--color-rule)); background:var(--color-active-muted); color:var(--color-active); }
  .seg-btn.needs-review { border-color:color-mix(in srgb,var(--color-warning) 35%,var(--color-rule)); background:var(--color-warning-muted); color:var(--color-warning); }
  .seg-btn:disabled { opacity:.6; cursor:not-allowed; }
  .note { margin:0; border:1px solid color-mix(in srgb,var(--color-warning) 25%,var(--color-rule)); background:var(--color-warning-muted); color:color-mix(in srgb,var(--color-warning) 80%,#221606); padding:.5rem .6rem; font-size:.84rem; border-radius:.45rem; }
  .note :global(a) { color:var(--color-active); font-weight:600; }
  .translator-notes { margin:.1rem 0 0; border-left:2px solid var(--color-clay); padding:.15rem 0 .15rem .7rem; color:var(--color-ink-muted); font-family:var(--font-editorial,Georgia,serif); font-size:.84rem; line-height:1.5; }
  .translator-notes p { margin:0; }
  .translator-notes p + p { margin-top:.42rem; }
  .translator-notes strong { color:var(--color-ink-soft); font-family:var(--font-sans,system-ui,sans-serif); font-size:.72rem; letter-spacing:.02em; }
  .error-note { margin:0; color:var(--color-danger); font-size:.83rem; }
  .draft-conflict { display:flex; flex-wrap:wrap; align-items:center; gap:.45rem; margin-top:.55rem; padding:.5rem; border:1px solid color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); border-radius:.35rem; color:var(--color-danger); font-size:.78rem; }
  @media (max-width:1100px) { .segment-row { grid-template-columns:1fr; } .source-col { border-right:none; border-bottom:1px solid var(--color-rule); } }
</style>
