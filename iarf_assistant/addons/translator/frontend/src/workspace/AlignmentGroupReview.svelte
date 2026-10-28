<script>
  import { tick } from 'svelte';
  import { alignmentNotePositions } from '../lib/alignment-notes.js';
  import AnnotatedSentence from './AnnotatedSentence.svelte';
  export let alignment = null;
  export let blockId = '';
  export let notes = [];
  export let blockNumber = 1;
  export let targetVersion = 0;
  export let editable = false;
  export let saving = false;
  export let onSentenceEdit = async () => ({ ok: false });
  export let onEditingChange = () => {};

  let editingOrder = null;
  let openedVersion = 0;
  let originalText = '';
  let editText = '';
  let editError = '';
  let editorElement;
  let splitOffset = 0;

  async function beginEdit(sentence) {
    if (!editable || saving || editingOrder !== null) return;
    editingOrder = sentence.order;
    openedVersion = targetVersion;
    originalText = sentence.text.trim();
    editText = originalText;
    editError = '';
    onEditingChange(true);
    await tick();
    editorElement?.focus();
  }

  function closeEditor() {
    if (saving) return;
    const order = editingOrder;
    editingOrder = null;
    editError = '';
    onEditingChange(false);
    tick().then(() => document.querySelector(`[data-block-id="${blockId}"] .target-side [data-sentence-order="${order}"] .sentence-text`)?.focus());
  }

  function rememberCursor() {
    // Textarea offsets use UTF-16; the persisted text contract counts Unicode characters.
    const leading = editText.length - editText.trimStart().length;
    splitOffset = Array.from(editText.slice(leading, Math.max(leading, editorElement?.selectionStart || 0))).length;
  }

  async function submitEdit(action) {
    if (!editable || saving || editingOrder === null) return;
    editError = '';
    const edit = { action, sentence_order: editingOrder };
    if (action !== 'merge_next') edit.text = editText.trim();
    if (action === 'split') edit.offset = splitOffset;
    const result = await onSentenceEdit(edit, openedVersion);
    if (result?.ok) { await tick(); closeEditor(); }
    else editError = result?.error || 'The sentence could not be saved. Your edit is still here.';
  }

  function editorKeydown(event) {
    if (event.key === 'Escape') { event.preventDefault(); closeEditor(); }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void submitEdit('replace');
    }
  }

  const kindLabels = {
    one_to_one: "Sentence pair",
    split: "One source sentence split into several translated sentences",
    merge: "Several source sentences merged into one translated sentence",
    many_to_many: "Several source and translated sentences aligned together"
  };

  function sentenceRange(sentences) {
    const first = sentences[0]?.order;
    const last = sentences.at(-1)?.order;
    return first === last ? first : `${first}–${last}`;
  }

  function moveBetweenGroups(event) {
    if (event.target !== event.currentTarget) return;
    if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    const rows = [...document.querySelectorAll('[data-alignment-group-row="true"]')];
    const index = rows.indexOf(event.currentTarget);
    const nextIndex = index + (event.key === 'ArrowDown' ? 1 : -1);
    const next = rows[nextIndex];
    if (!next) return;
    event.preventDefault();
    next.focus({ preventScroll: true });
    next.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }

  $: groups = Array.isArray(alignment?.groups) ? alignment.groups : [];
  $: notePositions = alignmentNotePositions(groups, notes);
  $: targetCount = groups.reduce((count, group) => count + group.target_sentences.length, 0);
  $: splitCharacters = Array.from(editText.trim());
  $: canSplit = splitOffset > 0 && splitOffset < splitCharacters.length
    && splitCharacters.slice(0, splitOffset).join('').trim()
    && splitCharacters.slice(splitOffset).join('').trim();
</script>

<section class="alignment-review" aria-label={`Sentence alignment for block ${blockNumber}`}>
  <div class="alignment-groups" role="grid" aria-label={`Alignment groups for block ${blockNumber}`}>
    {#each groups as group, index (group.order)}
      <div role="presentation">
        <div
          class="alignment-group"
          tabindex="0"
          role="row"
          data-alignment-group-row="true"
          data-alignment-group-order={group.order}
          aria-label={`Block ${blockNumber}, alignment group ${group.order}`}
          on:keydown={moveBetweenGroups}
        >
          <div class="group-marker" role="gridcell" title={kindLabels[group.kind] || "Alignment group"}>
            <span>{group.order}</span>
            {#if group.kind !== "one_to_one"}<small>{group.source_sentences.length}:{group.target_sentences.length}</small>{/if}
          </div>
          <div class="group-side source-side" role="gridcell" aria-label={`Source sentences ${sentenceRange(group.source_sentences)} in alignment group ${group.order}`}>
            {#if index === 0}<span class="side-label">Source</span>{/if}
            {#each group.source_sentences as sentence (sentence.order)}
              <p dir="auto" data-sentence-order={sentence.order}><AnnotatedSentence text={sentence.text} entries={notePositions.source.get(sentence.order) || []} {blockId} /></p>
            {/each}
          </div>
          <div class="group-side target-side" role="gridcell" aria-label={`Translated sentences ${sentenceRange(group.target_sentences)} in alignment group ${group.order}`}>
            {#if index === 0}<span class="side-label">Translation</span>{/if}
            {#each group.target_sentences as sentence (sentence.order)}
              {#if editingOrder === sentence.order}
                <form class="sentence-editor" aria-label={`Edit translated sentence ${sentence.order}`} on:submit|preventDefault={() => submitEdit('replace')}>
                  <label class="editor-label" for={`sentence-editor-${blockId}`}>Translation · sentence {sentence.order}</label>
                  <textarea id={`sentence-editor-${blockId}`} bind:this={editorElement} bind:value={editText} rows={Math.min(9, Math.max(3, Math.ceil(editText.length / 65)))} dir="auto" readonly={!editable || saving} on:select={rememberCursor} on:click={rememberCursor} on:keyup={rememberCursor} on:input={rememberCursor} on:keydown={editorKeydown}></textarea>
                  {#if openedVersion !== targetVersion}<p class="edit-warning">This block has changed since you opened the sentence. Copy your draft before cancelling and reopening it.</p>{/if}
                  {#if editError}<p class="edit-warning" role="alert">{editError}</p>{/if}
                  <div class="sentence-actions">
                    <button class="save-sentence" type="submit" disabled={!editable || saving || !editText.trim()}>{saving ? 'Saving…' : 'Save'}</button>
                    <button type="button" disabled={saving} on:click={closeEditor}>Cancel</button>
                    <span class="action-spacer"></span>
                    <button type="button" disabled={!editable || saving || !canSplit} title="Place the cursor where this sentence should split" on:click={() => submitEdit('split')}>Split at cursor</button>
                    <button type="button" disabled={!editable || saving || editingOrder >= targetCount || editText.trim() !== originalText} title={editText.trim() !== originalText ? 'Save wording changes before merging' : 'Join this sentence and the next, keeping their text'} on:click={() => submitEdit('merge_next')}>Merge with next</button>
                  </div>
                </form>
              {:else}
                <p dir="auto" data-sentence-order={sentence.order}><AnnotatedSentence text={sentence.text} entries={notePositions.target.get(sentence.order) || []} {blockId} editable={editable && editingOrder === null && !saving} editLabel={`Edit translated sentence ${sentence.order}`} onEdit={() => beginEdit(sentence)} /></p>
              {/if}
            {/each}
          </div>
        </div>
      </div>
    {/each}
  </div>
</section>

<style>
  .alignment-review { display:grid; gap:0; }
  .alignment-groups { display:grid; margin:0; padding:0; list-style:none; }
  .alignment-groups > div + div { border-top:1px solid color-mix(in srgb,var(--color-rule,#d0c8bc) 36%,transparent); }
  .alignment-group { display:grid; grid-template-columns:2.5rem minmax(0,1fr) minmax(0,1fr); min-width:0; outline:0; }
  .alignment-group:hover { background:color-mix(in srgb,var(--color-paper,#f1eee7) 20%,transparent); }
  .alignment-group:focus-visible { box-shadow:inset 3px 0 0 color-mix(in srgb,var(--color-clay,#7c6053) 58%,transparent); background:color-mix(in srgb,var(--color-paper,#f1eee7) 32%,transparent); }
  .group-marker { display:grid; align-content:start; justify-items:start; gap:.12rem; padding:.85rem .25rem .85rem 1rem; color:var(--color-ink-muted,#675f56); }
  .group-marker span { font-size:.65rem; font-variant-numeric:tabular-nums; }
  .group-marker small { font-size:.6rem; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .group-side { min-width:0; padding:.7rem 1rem .9rem; }
  .source-side { border-right:1px solid color-mix(in srgb,var(--color-rule,#d0c8bc) 55%,transparent); }
  .side-label { display:block; margin-bottom:.34rem; color:var(--color-ink-muted,#675f56); font-size:.57rem; font-weight:750; letter-spacing:.1em; text-transform:uppercase; }
  .group-side p { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--color-ink,#302d29); font-family:var(--font-editorial,Georgia,serif); font-size:.96rem; line-height:1.58; unicode-bidi:plaintext; }
  .group-side p + p { margin-top:.48rem; padding-top:.48rem; border-top:1px dotted color-mix(in srgb,var(--color-rule,#d0c8bc) 72%,transparent); }
  .sentence-editor { display:grid; gap:.5rem; margin:.2rem 0 .7rem; }
  .editor-label { color:var(--color-ink-muted,#675f56); font-size:.65rem; }
  .sentence-editor textarea { box-sizing:border-box; width:100%; min-height:5.5rem; resize:vertical; padding:.6rem; border:1px solid var(--color-rule-strong,#968b7d); border-radius:3px; background:var(--color-surface,#faf8f3); color:var(--color-ink,#302d29); font:inherit; font-family:var(--font-editorial,Georgia,serif); line-height:1.58; }
  .sentence-editor textarea:focus-visible { outline:2px solid var(--color-clay,#7c6053); outline-offset:1px; }
  .sentence-actions { display:flex; flex-wrap:wrap; align-items:center; gap:.3rem; }
  .sentence-actions button { padding:.35rem .5rem; border:1px solid var(--color-rule,#d0c8bc); border-radius:3px; background:var(--color-surface,#faf8f3); color:var(--color-ink,#302d29); font:inherit; font-size:.7rem; cursor:pointer; }
  .sentence-actions button:disabled { opacity:.5; cursor:not-allowed; }
  .sentence-actions .save-sentence { color:var(--color-surface,#faf8f3); background:var(--color-clay,#7c6053); border-color:var(--color-clay,#7c6053); }
  .sentence-actions .action-spacer { flex:1; }
  .group-side .edit-warning { color:var(--color-warning,#9a6a18); font:inherit; font-size:.75rem; }
  @media (max-width:560px) {
    .alignment-group { grid-template-columns:2.8rem minmax(0,1fr); }
    .group-marker { grid-row:1 / span 2; padding-left:.9rem; }
    .source-side { border-right:0; border-bottom:1px solid var(--color-rule,#d0c8bc); }
    .target-side { grid-column:2; }
  }
  @media (max-width:620px) {
    .group-marker { padding-left:.72rem; }
    .group-side { padding:.8rem .82rem .9rem; }
  }
</style>
