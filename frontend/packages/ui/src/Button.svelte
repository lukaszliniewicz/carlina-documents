<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLButtonAttributes } from "svelte/elements";

  import { classNames } from "./utilities.ts";

  type Props = Omit<HTMLButtonAttributes, "class"> & {
    children: Snippet;
    class?: string;
    variant?: "primary" | "quiet" | "outline" | "danger";
    size?: "sm" | "md";
  };

  let {
    children,
    variant = "primary",
    size = "md",
    class: className = "",
    ...rest
  }: Props = $props();
</script>

<button
  {...rest}
  class={classNames("ui-button", `ui-button--${variant}`, `ui-button--${size}`, className)}
>
  {@render children()}
</button>

<style>
  .ui-button {
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    border: 1px solid transparent;
    padding: 0.62rem 0.9rem;
    cursor: pointer;
    font-size: 0.78rem;
    font-weight: 650;
    letter-spacing: 0.025em;
    line-height: 1;
    transition:
      background var(--shell-transition),
      border-color var(--shell-transition),
      color var(--shell-transition);
  }

  .ui-button:disabled {
    cursor: not-allowed;
    opacity: 0.52;
  }

  .ui-button--sm {
    min-height: 2.1rem;
    padding: 0.48rem 0.68rem;
    font-size: 0.73rem;
  }

  .ui-button--primary {
    border-color: var(--color-umber);
    background: var(--color-umber);
    color: var(--color-surface);
  }

  .ui-button--primary:hover:not(:disabled) {
    background: var(--color-ink-soft);
  }

  .ui-button--quiet {
    background: transparent;
    color: var(--color-ink-soft);
  }

  .ui-button--quiet:hover:not(:disabled) {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .ui-button--outline {
    border-color: var(--color-rule-strong);
    background: var(--color-surface);
    color: var(--color-ink);
  }

  .ui-button--outline:hover:not(:disabled) {
    background: var(--color-surface-muted);
  }

  .ui-button--danger {
    border-color: var(--color-danger);
    background: var(--color-danger);
    color: var(--color-surface);
  }
</style>
