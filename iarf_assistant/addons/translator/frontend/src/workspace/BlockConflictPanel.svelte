<script>
  import { compactContentText } from "./compact-editor.js";

  export let conflict = null;
  export let onReload = () => {};

  $: latestCandidateText = compactContentText(conflict?.block?.target?.editor?.content || "");
</script>

{#if conflict}
  <aside class="conflict" role="alert" data-testid="block-conflict">
    <div>
      <strong>Server version changed</strong>
      <p>Your local draft is preserved. Save is blocked until you explicitly reload the server version; there is no silent rebase.</p>
      {#if latestCandidateText}<p class="latest-preview"><strong>Server candidate:</strong> {latestCandidateText}</p>{/if}
      {#if conflict.error}<p class="error-detail">{conflict.error}</p>{/if}
    </div>
    <div class="actions">
      <button type="button" class="btn btn-primary" on:click={() => onReload(conflict.block)}>Reload server</button>
    </div>
  </aside>
{/if}

<style>
  .conflict { display:flex; align-items:start; justify-content:space-between; gap:.8rem; margin:.7rem 0; padding:.75rem; border:1px solid color-mix(in srgb,var(--color-danger,#b33) 45%,var(--color-rule,#ddd)); background:var(--color-danger-muted,#fff0f0); color:var(--color-ink-soft,#555); }
  strong { color:var(--color-danger,#b33); }
  p { margin:.25rem 0 0; font-size:.82rem; }
  .error-detail { color:var(--color-danger,#b33); font-size:.75rem; }
  .actions { display:flex; gap:.4rem; flex-wrap:wrap; flex-shrink:0; }
  .btn { border:1px solid var(--color-rule,#ddd); padding:.4rem .65rem; cursor:pointer; font:inherit; font-size:.78rem; }
  .btn-primary { color:var(--color-surface,#fff); background:var(--color-umber,#5c5149); border-color:var(--color-umber,#5c5149); }
  @media (max-width: 700px) { .conflict { flex-direction:column; } }
</style>
