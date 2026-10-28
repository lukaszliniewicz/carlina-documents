<script lang="ts">
  import type { Snippet } from "svelte";

  import IconButton from "./IconButton.svelte";

  interface Props {
    open: boolean;
    title: string;
    description?: string;
    children: Snippet;
    footer?: Snippet;
    onclose: () => void;
    size?: "sm" | "md" | "lg";
  }

  let {
    open,
    title,
    description,
    children,
    footer,
    onclose,
    size = "md",
  }: Props = $props();

  let dialog: HTMLDialogElement;
  let restoreTarget: HTMLElement | null = null;
  const titleId = `dialog-title-${Math.random().toString(36).slice(2)}`;
  const descriptionId = `${titleId}-description`;

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      restoreTarget =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  function restoreFocus() {
    queueMicrotask(() => restoreTarget?.focus());
  }

  function requestClose() {
    onclose();
    restoreFocus();
  }
</script>

<dialog
  bind:this={dialog}
  class:size-sm={size === "sm"}
  class:size-lg={size === "lg"}
  aria-labelledby={titleId}
  aria-describedby={description ? descriptionId : undefined}
  oncancel={(event) => {
    event.preventDefault();
    requestClose();
  }}
  onclose={restoreFocus}
>
  <header>
    <div>
      <p class="eyebrow">Carlina</p>
      <h2 id={titleId}>{title}</h2>
      {#if description}
        <p id={descriptionId} class="description">{description}</p>
      {/if}
    </div>
    <IconButton
      class="dialog-close"
      icon="close"
      label="Close dialog"
      onclick={requestClose}
    />
  </header>
  <div class="dialog-body">
    {@render children()}
  </div>
  {#if footer}
    <footer>{@render footer()}</footer>
  {/if}
</dialog>

<style>
  dialog {
    grid-template-rows: auto minmax(0, 1fr) auto;
    width: min(38rem, calc(100vw - 2rem));
    max-height: min(48rem, calc(100vh - 2rem));
    margin: auto;
    border: 1px solid var(--color-rule-strong);
    padding: 0;
    overflow: hidden;
    background: var(--color-surface);
    color: var(--color-ink);
    box-shadow: 12px 14px 0 color-mix(in srgb, var(--color-ink) 10%, transparent);
  }

  dialog[open] {
    display: grid;
  }

  dialog.size-sm {
    width: min(28rem, calc(100vw - 2rem));
  }

  dialog.size-lg {
    width: min(52rem, calc(100vw - 2rem));
  }

  dialog::backdrop {
    background: color-mix(in srgb, var(--color-ink) 38%, transparent);
    backdrop-filter: blur(2px);
  }

  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.1rem 1.1rem 1rem 1.25rem;
  }

  :global(.dialog-close.ui-icon-button) {
    width: 2.75rem;
    height: 2.75rem;
  }

  .eyebrow {
    margin: 0 0 0.3rem;
    color: var(--color-clay);
    font-size: 0.61rem;
    font-weight: 800;
    letter-spacing: 0.18em;
    text-transform: uppercase;
  }

  h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.55rem;
    font-weight: 500;
  }

  .description {
    margin: 0.35rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.82rem;
    line-height: 1.5;
  }

  .dialog-body {
    min-height: 0;
    overflow: auto;
    padding: 1.25rem;
  }

  footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.55rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.9rem 1.25rem;
    background: var(--color-paper);
  }
</style>
