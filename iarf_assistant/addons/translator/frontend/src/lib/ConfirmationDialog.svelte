<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let title = "Confirm action";
  export let description = "";
  export let confirmLabel = "Confirm";
  export let busy = false;
  export let destructive = true;
  export let onconfirm = () => {};
  export let onclose = () => {};
</script>

<Dialog {open} {title} {description} onclose={onclose}>
  <div class="confirmation-copy">{description}</div>
  <div class="confirmation-actions">
    <button class="btn btn-ghost" type="button" disabled={busy} on:click={onclose}>Cancel</button>
    <button class:btn-danger={destructive} class:btn-primary={!destructive} class="btn" type="button" disabled={busy} on:click={onconfirm}>{busy ? "Working..." : confirmLabel}</button>
  </div>
</Dialog>

<style>
  .confirmation-copy { color:var(--color-ink-muted,#667); line-height:1.5; }
  .confirmation-actions { display:flex; justify-content:flex-end; gap:.5rem; margin-top:1rem; }
  .btn { border:1px solid transparent; border-radius:.35rem; padding:.55rem .9rem; cursor:pointer; font:inherit; }
  .btn-ghost { background:transparent; border-color:var(--color-rule,#ddd); color:var(--color-ink-muted,#667); }
  .btn-danger { background:var(--color-danger-muted,#fff0f0); border-color:color-mix(in srgb,var(--color-danger,#b33) 35%,var(--color-rule,#ddd)); color:var(--color-danger,#b33); }
  .btn-primary { background:var(--color-umber,#5c5149); border-color:var(--color-umber,#5c5149); color:var(--color-surface,#faf8f3); }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
</style>
