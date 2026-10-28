<script lang="ts">
  import type { Snippet } from "svelte";

  import Icon, { type IconName } from "./Icon.svelte";

  interface Props {
    title: string;
    description: string;
    icon?: IconName;
    actions?: Snippet;
  }

  let {
    title,
    description,
    icon = "workspaces",
    actions,
  }: Props = $props();
</script>

<div class="empty-state">
  <span class="empty-state__icon"><Icon name={icon} size={22} /></span>
  <div>
    <h2>{title}</h2>
    <p>{description}</p>
  </div>
  {#if actions}
    <div class="empty-state__actions">{@render actions()}</div>
  {/if}
</div>

<style>
  .empty-state {
    min-height: 15rem;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 1rem;
    border-block: 1px solid var(--color-rule);
    padding: 2.5rem clamp(1rem, 5vw, 4rem);
    background: color-mix(in srgb, var(--color-surface) 72%, transparent);
  }

  .empty-state__icon {
    display: grid;
    width: 2.8rem;
    height: 2.8rem;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
  }

  h2 {
    margin: 0 0 0.35rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.35rem, 3vw, 1.8rem);
    font-weight: 500;
  }

  p {
    max-width: 34rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.88rem;
    line-height: 1.65;
  }

  .empty-state__actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
  }
</style>
