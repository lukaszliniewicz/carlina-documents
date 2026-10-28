<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let term = {};
  export let busy = false;
  export let onClose = () => {};
  export let onSave = () => {};
  export let onFieldChange = () => {};

  function setField(field, value) {
    onFieldChange(field, value);
  }
</script>

<Dialog
  {open}
  title={term.id ? "Edit Term" : "Add Term"}
  onclose={() => !busy && onClose()}
  size="sm"
>
  <form class="modal-form" on:submit={onSave}>
    <label>
      Source Term
      <input value={term.source_term || ""} on:input={(event) => setField("source_term", event.currentTarget.value)} />
    </label>
    <label>
      Target Term
      <input value={term.target_term || ""} on:input={(event) => setField("target_term", event.currentTarget.value)} />
    </label>
    <label>
      Notes
      <textarea rows="3" value={term.context_notes || ""} on:input={(event) => setField("context_notes", event.currentTarget.value)}></textarea>
    </label>
    <label class="checkbox">
      <input type="checkbox" checked={Boolean(term.needs_review)} on:change={(event) => setField("needs_review", event.currentTarget.checked)} />
      Mark for review
    </label>
    <button class="btn primary" type="submit" disabled={busy}>{busy ? "Saving..." : "Save Term"}</button>
  </form>
</Dialog>

<style>
  .modal-form { display:flex; flex-direction:column; gap:.72rem; margin-top:.1rem; }
  .modal-form label { display:flex; flex-direction:column; gap:.35rem; font-size:.9rem; color:var(--color-ink-soft); }
  .modal-form label.checkbox { flex-direction:row; align-items:center; gap:.5rem; }
  input:not([type="checkbox"]):not([type="radio"]), textarea { width:100%; box-sizing:border-box; border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); border-radius:.5rem; padding:.55rem .6rem; font:inherit; }
  input:not([type="checkbox"]):not([type="radio"]):focus, textarea:focus { outline:2px solid color-mix(in srgb,var(--color-active) 20%,transparent); border-color:color-mix(in srgb,var(--color-active) 38%,var(--color-rule)); outline-offset:1px; }
  .btn { border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); padding:.46rem .76rem; cursor:pointer; font-size:.86rem; border-radius:.48rem; }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
</style>
