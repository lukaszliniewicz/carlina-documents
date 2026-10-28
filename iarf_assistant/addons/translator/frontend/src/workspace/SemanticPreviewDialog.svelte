<script>
  export let previewOpen = false;
  export let previewLoading = false;
  export let previewHtml = "";
  export let previewError = "";
  export let onClosePreview = () => {};

  let previewDialog = null;
  $: if (previewOpen && previewDialog && !previewDialog.open) {
    try {
      previewDialog.showModal();
    } catch {
      // A non-browser test host may not implement modal dialogs. The open
      // fallback still keeps the preview isolated by the iframe sandbox.
      previewDialog.open = true;
    }
  }
  $: if (!previewOpen && previewDialog?.open) previewDialog.close();
</script>

{#if previewOpen}
  <dialog bind:this={previewDialog} class="preview-dialog" aria-modal="true" aria-labelledby="semantic-preview-title" on:close={onClosePreview}>
    <div class="preview-card">
      <header class="preview-header">
        <h2 id="semantic-preview-title">Document preview</h2>
        <button type="button" class="close-button" aria-label="Close document preview" disabled={previewLoading} on:click={onClosePreview}>Close</button>
      </header>
      {#if previewLoading}
        <p class="preview-status" role="status">Generating preview...</p>
      {:else if previewError}
        <p class="preview-status error" role="alert">{previewError}</p>
      {:else}
        <iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="Semantic translation preview" srcdoc={previewHtml}></iframe>
      {/if}
    </div>
  </dialog>
{/if}

<style>
  p { margin:0; }
  .close-button { border:1px solid var(--color-rule,#ddd); border-radius:.4rem; padding:.45rem .7rem; background:var(--color-paper-muted,#f6f6f6); color:var(--color-ink-soft,#555); cursor:pointer; font:inherit; font-size:.8rem; white-space:nowrap; }
  .close-button:disabled { opacity:.6; cursor:not-allowed; }
  .preview-dialog { position:fixed; inset:0; box-sizing:border-box; width:100vw; height:100dvh; max-width:none; max-height:none; margin:0; padding:0; border:0; background:var(--color-surface,#fff); }
  .preview-dialog::backdrop { background:rgba(20,28,40,.58); }
  .preview-card { display:flex; flex-direction:column; height:100%; min-height:0; background:var(--color-surface,#fff); color:var(--color-ink,#222); }
  .preview-header { display:flex; justify-content:space-between; align-items:center; gap:1rem; padding:.65rem 1rem; border-bottom:1px solid var(--color-rule,#ddd); }
  h2 { margin:0; font-size:1.1rem; }
  iframe { display:block; flex:1; width:100%; min-height:0; border:0; background:var(--color-surface,#fff); }
  .preview-status { margin:0; padding:1rem; border:1px solid var(--color-rule,#ddd); color:var(--color-ink-muted,#667); }
  .preview-status.error { border-color:color-mix(in srgb,var(--color-danger,#b33) 35%,var(--color-rule,#ddd)); color:var(--color-danger,#b33); background:var(--color-danger-muted,#fff0f0); }
</style>
