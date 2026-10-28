<script>
  import { noteElementId } from '../lib/alignment-notes.js';
  export let blockId;
  export let entry;
  $: id = noteElementId(blockId, entry.note.id);
</script>

<sup><a href={`#${id}`} data-carlina-note={id} aria-label={`Note ${entry.number}${entry.note.state === 'needs_review' ? ' · anchor needs review' : entry.note.state === 'proposed' ? ' · proposed' : ''}`} aria-haspopup="dialog" aria-expanded="false">[T{entry.number}]</a></sup>

<style>
  sup { line-height:0; vertical-align:super; font-size:.72em; }
  a { display:inline-block; padding:.25rem .18rem; color:var(--color-clay,#7c6053); font-family:system-ui,sans-serif; font-weight:700; text-decoration:underline; text-underline-offset:2px; }
  a:hover, a:focus-visible { color:var(--color-ink,#302d29); background:var(--color-paper,#f1eee7); outline:2px solid var(--color-clay,#7c6053); outline-offset:2px; border-radius:2px; }
</style>
