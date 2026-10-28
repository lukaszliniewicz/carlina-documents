<script>
  import BlockCard from "./BlockCard.svelte";

  export let blocks = [];
  export let readOnly = false;
  export let accessRole = "owner";
  export let reviewMode = "block";
  export let drafts = {};
  export let savingBlocks = new Set();
  export let conflicts = {};
  export let totalBlocks = 0;
  export let structuralDisabled = false;
  export let structuralBusy = false;
  export let selectionMode = false;
  export let selectedBlockIds = new Set();
  export let selectionDisabled = false;
  export let onCandidateChange = () => {};
  export let onSave = () => {};
  export let onSentenceEdit = async () => ({ ok: false });
  export let onSentenceEditingChange = () => {};
  export let onReloadConflict = () => {};
  export let onCorrect = () => {};
  export let onSelect = () => {};
  export let noteBusy = new Set();
  export let noteErrors = {};
  export let onCreateNote = () => {};
  export let onReviseNote = () => {};
  export let onTransitionNote = () => {};
  export let onReview = () => {};
  export let onCoverage = () => {};
</script>

{#if blocks.length}
  <div class="block-list" aria-label="Ordered semantic blocks">
    {#each blocks as block (block.id)}
      <BlockCard
        {block}
        {readOnly}
        {accessRole}
        reviewMode={reviewMode}
        draft={drafts[block.id] || null}
        saving={savingBlocks.has(block.id)}
        conflict={conflicts[block.id] || null}
        canMergePrevious={Number(block.order) > 1}
        canMergeNext={Number(block.order) < Number(totalBlocks)}
        {structuralDisabled}
        {structuralBusy}
        {selectionMode}
        selected={selectedBlockIds.has(block.id)}
        {selectionDisabled}
        {onCandidateChange}
        {onSave}
        {onSentenceEdit}
        {onSentenceEditingChange}
        onReloadConflict={(latest) => onReloadConflict(block.id, latest)}
        {onCorrect}
        {onSelect}
        noteBusy={noteBusy.has(block.id)}
        noteError={noteErrors[block.id] || ""}
        onCreateNote={(payload) => onCreateNote(block, payload)}
        onReviseNote={(noteId, payload, version) => onReviseNote(block, noteId, payload, version)}
        onTransitionNote={(noteId, state, version) => onTransitionNote(block, noteId, state, version)}
        onReview={(decision) => onReview(block, decision)}
        onCoverage={(kind) => onCoverage(block, kind)}
      />
    {/each}
  </div>
{:else}
  <div class="empty-blocks" data-testid="block-empty-state">No semantic blocks are available yet.</div>
{/if}

<style>
  .block-list { display:grid; gap:.85rem; }
  .empty-blocks { padding:2rem 1rem; border:1px dashed var(--color-rule,#ddd); border-radius:.55rem; color:var(--color-ink-muted,#667); text-align:center; }
</style>
