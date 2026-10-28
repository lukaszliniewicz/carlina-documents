<script lang="ts">
  import type { HTMLButtonAttributes } from "svelte/elements";

  import { classNames } from "./utilities.ts";
  import Icon, { type IconName } from "./Icon.svelte";

  type Props = Omit<HTMLButtonAttributes, "class"> & {
    class?: string;
    icon: IconName;
    label: string;
    size?: "sm" | "md";
    selected?: boolean;
  };

  let {
    icon,
    label,
    size = "md",
    selected = false,
    class: className = "",
    ...rest
  }: Props = $props();
</script>

<button
  {...rest}
  class={classNames("ui-icon-button", `ui-icon-button--${size}`, selected && "is-selected", className)}
  aria-label={label}
  aria-pressed={rest["aria-pressed"] ?? (selected ? "true" : undefined)}
  title={rest.title ?? label}
>
  <Icon name={icon} size={size === "sm" ? 16 : 18} />
</button>

<style>
  .ui-icon-button {
    width: 2.5rem;
    height: 2.5rem;
    display: inline-grid;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid transparent;
    background: transparent;
    color: var(--color-ink-soft);
    cursor: pointer;
    transition:
      background var(--shell-transition),
      border-color var(--shell-transition),
      color var(--shell-transition);
  }

  .ui-icon-button--sm {
    width: 2.1rem;
    height: 2.1rem;
  }

  .ui-icon-button:hover,
  .ui-icon-button.is-selected {
    border-color: var(--color-rule);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .ui-icon-button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
</style>
