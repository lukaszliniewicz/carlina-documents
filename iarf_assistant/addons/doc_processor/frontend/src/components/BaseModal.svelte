<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";

  export let open = false;
  export let title = "";
  export let size: "small" | "medium" | "large" | "full" = "medium";
  export let bodyClass = "";

  const dispatch = createEventDispatcher<{ close: void }>();
  const titleId = `doc-processor-dialog-${Math.random().toString(36).slice(2)}`;
  let dialog: HTMLDialogElement;
  let restoreTarget: HTMLElement | null = null;

  function close() {
    dispatch("close");
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
    <button class="dp-icon-btn" type="button" aria-label="Close" title="Close" on:click={close}>
      <Icon name="close" size={17} />
    </button>
  </header>
  <div class={`dp-modal-body ${bodyClass}`.trim()}>
    <slot />
  </div>
  <slot name="footer" />
</dialog>
