<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let exportOptions = [];
  export let exportFormat = "html";
  export let exportBusy = false;
  export let exportStatus = "";
  export let onClose = () => {};
  export let onSubmit = () => {};
  export let onFormatChange = () => {};
</script>

<Dialog
  {open}
  title="Export Translation"
  description="Choose a format supported by this workspace."
  onclose={() => !exportBusy && onClose()}
>
  <form class="modal-form" on:submit={onSubmit}>
    <fieldset class="export-options checklist">
      <legend>Choose export format</legend>
      {#each exportOptions as option}
        <label class="check-item">
          <input
            class="check-input"
            type="radio"
            checked={exportFormat === option.value}
            value={option.value}
            on:change={(event) => onFormatChange(event.currentTarget.value)}
          />
          <span class="check-copy">
            <span class="check-title">{option.label}</span>
            <span class="check-meta">{option.extension}</span>
          </span>
        </label>
      {/each}
    </fieldset>
    {#if exportStatus}<p class="hint">{exportStatus}</p>{/if}
    <button class="btn primary" type="submit" disabled={exportBusy}>
      {exportBusy ? "Preparing..." : "Download"}
    </button>
  </form>
</Dialog>

<style>
  .modal-form { display: flex; flex-direction: column; gap: 0.72rem; }
  .checklist { display: flex; flex-direction: column; gap: 0.5rem; }
  .check-item { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: start; gap: 0.62rem; padding: 0.55rem 0.62rem; border: 1px solid var(--color-rule); border-radius: 0.55rem; background: var(--color-surface); cursor: pointer; transition: border-color 120ms ease, background-color 120ms ease; }
  .check-item:hover { border-color: color-mix(in srgb, var(--color-active) 26%, var(--color-rule)); background: color-mix(in srgb, var(--color-active-muted) 52%, var(--color-surface)); }
  .check-item:focus-within { outline: 2px solid color-mix(in srgb, var(--color-active) 22%, transparent); outline-offset: 1px; }
  .check-input { width: 1rem; height: 1rem; margin: 0.13rem 0 0; accent-color: var(--color-active); }
  .check-copy { min-width: 0; display: flex; flex-direction: column; gap: 0.12rem; }
  .check-title { font-size: 0.88rem; color: var(--color-ink-soft); line-height: 1.35; }
  .check-meta, .hint { font-size: 0.75rem; color: var(--color-ink-muted); }
  .hint { margin: 0; font-size: 0.84rem; }
  .btn { border: 1px solid var(--color-rule); background: var(--color-surface); color: var(--color-ink); padding: 0.46rem 0.76rem; cursor: pointer; font-size: 0.86rem; border-radius: 0.48rem; }
  .btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .btn.primary { border-color: var(--color-active); background: var(--color-active); color: var(--color-surface); }
</style>
