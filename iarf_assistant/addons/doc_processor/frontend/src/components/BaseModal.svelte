<script lang="ts">
  import { Icon, confirmDiscardChanges, guardUnsavedChanges } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";

  export let open = false;
  export let title = "";
  export let size: "small" | "medium" | "large" | "full" = "medium";
  export let bodyClass = "";
  export let dirty = false;
  export let busy = false;

  const dispatch = createEventDispatcher<{ close: void }>();
  const titleId = `doc-processor-dialog-${Math.random().toString(36).slice(2)}`;
  let dialog: HTMLDialogElement;
  let restoreTarget: HTMLElement | null = null;

  async function close() {
    if (await confirmDiscardChanges({ dirty, busy }) && open && !busy) dispatch("close");
  }

  function restoreFocus() {
    queueMicrotask(() => restoreTarget?.focus());
  }

  function syncDialog(nextOpen: boolean) {
    if (!dialog) return;
    if (nextOpen && !dialog.open) {
      restoreTarget = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      dialog.showModal();
    } else if (!nextOpen && dialog.open) {
      dialog.close();
    }
  }

  $: if (dialog) syncDialog(open);
</script>

<dialog
  bind:this={dialog}
  use:guardUnsavedChanges={{ dirty: open && dirty, busy: open && busy }}
  class="dp-modal dp-modal-{size}"
  aria-labelledby={titleId}
  on:cancel={(event) => {
    event.preventDefault();
    close();
  }}
  on:close={restoreFocus}
  on:click={(event) => {
    if (event.target === dialog) close();
  }}
>
  <header class="dp-modal-header">
    <div class="dp-modal-title-row">
      <h2 id={titleId}>{title}</h2>
      <div class="dp-modal-header-actions">
        <slot name="header-actions" />
      </div>
    </div>
    <button class="dp-icon-btn" type="button" aria-label="Close" title={busy ? "Wait for saving to finish" : "Close"} disabled={busy} on:click={close}>
      <Icon name="close" size={17} />
    </button>
  </header>
  <div class={`dp-modal-body ${bodyClass}`.trim()}>
    {#if open}<slot />{/if}
  </div>
  {#if open}<slot name="footer" />{/if}
</dialog>
