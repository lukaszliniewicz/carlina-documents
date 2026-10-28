<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let title = "Enter a value";
  export let description = "";
  export let label = "Value";
  export let placeholder = "";
  export let initialValue = "";
  export let confirmLabel = "Continue";
  export let onconfirm = () => {};
  export let onclose = () => {};
  let value = "";
  let wasOpen = false;

  $: if (open && !wasOpen) value = initialValue || "";
  $: wasOpen = open;

  async function submit(event) {
    event.preventDefault();
    const next = value.trim();
    if (!next) return;
    await onconfirm(next);
  }
</script>

<Dialog {open} {title} {description} onclose={onclose} size="sm">
  <form class="input-dialog" on:submit={submit}>
    <label>{label}<input bind:value placeholder={placeholder} /></label>
    <div class="input-actions">
      <button class="btn btn-ghost" type="button" on:click={onclose}>Cancel</button>
      <button class="btn btn-primary" type="submit" disabled={!value.trim()}>{confirmLabel}</button>
    </div>
  </form>
</Dialog>

<style>
  .input-dialog { display:flex; flex-direction:column; gap:1rem; }
  label { display:flex; flex-direction:column; gap:.35rem; color:var(--color-ink-soft,#555); }
  input { border:1px solid var(--color-rule,#ddd); border-radius:.35rem; padding:.55rem; font:inherit; }
  .input-actions { display:flex; justify-content:flex-end; gap:.5rem; }
  .btn { border:1px solid transparent; border-radius:.35rem; padding:.55rem .9rem; cursor:pointer; font:inherit; }
  .btn-ghost { background:transparent; border-color:var(--color-rule,#ddd); color:var(--color-ink-muted,#667); }
  .btn-primary { background:var(--color-active,#356ae6); border-color:var(--color-active,#356ae6); color:var(--color-surface,#fff); }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
</style>
