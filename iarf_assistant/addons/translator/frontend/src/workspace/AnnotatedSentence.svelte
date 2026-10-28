<script>
  import { sentenceNoteParts } from '../lib/alignment-notes.js';
  import NoteReference from './NoteReference.svelte';
  export let text;
  export let entries = [];
  export let blockId;
  export let editable = false;
  export let editLabel = '';
  export let onEdit = () => {};
  $: parts = sentenceNoteParts(text, entries);
  $: firstText = parts.findIndex((part) => part.text.trim());
</script>

{#each parts as part, index}{#if editable && part.text.trim()}<button type="button" class="sentence-text" aria-label={editLabel} tabindex={index === firstText ? 0 : -1} on:click={onEdit}>{part.text}</button>{:else}{part.text}{/if}{#if part.entry}<NoteReference {blockId} entry={part.entry} />{/if}{/each}

<style>
  .sentence-text { display:inline; margin:0; padding:0; border:0; border-radius:2px; background:transparent; color:inherit; font:inherit; line-height:inherit; text-align:start; white-space:pre-wrap; cursor:text; }
  .sentence-text:hover { background:color-mix(in srgb,var(--color-clay,#7c6053) 9%,transparent); }
  .sentence-text:focus-visible { outline:2px solid var(--color-clay,#7c6053); outline-offset:3px; }
</style>
