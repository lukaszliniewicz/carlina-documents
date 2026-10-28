<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let previewLoading = false;
  export let previewHtml = "Generating preview...";
  export let onClose = () => {};
</script>

<Dialog
  {open}
  title="Document Preview"
  description="Read-only preview of the current translation."
  onclose={onClose}
  size="lg"
>
  {#if previewLoading}<p class="hint">Generating preview...</p>{/if}
  <iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="Translation preview" srcdoc={previewHtml}></iframe>
</Dialog>

<style>
  .hint { margin:0 0 .75rem; color:var(--color-ink-muted); font-size:.84rem; }
  iframe { display:block; width:100%; min-height:28rem; border:1px solid var(--color-rule); background:var(--color-surface); }
</style>
