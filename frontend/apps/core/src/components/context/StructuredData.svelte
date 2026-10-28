<script lang="ts">
  import { structuredRows } from "../../lib/workspace-state.ts";

  interface Props {
    value: unknown;
    emptyText?: string;
    maxRows?: number;
    compact?: boolean;
  }

  let {
    value,
    emptyText = "No additional detail recorded.",
    maxRows = 60,
    compact = false,
  }: Props = $props();

  const rows = $derived(structuredRows(value, { maxRows }));
</script>

{#if rows.length}
  <dl class="structured-data" class:structured-data--compact={compact}>
    {#each rows as row (row.path)}
      <div class:structured-data__empty={row.kind === "empty"}>
        <dt>{row.label}</dt>
        <dd>{row.value}</dd>
      </div>
    {/each}
  </dl>
{:else}
  <p class="structured-data__empty-message">{emptyText}</p>
{/if}

<style>
  .structured-data {
    display: grid;
    gap: 1px;
    margin: 0;
    background: var(--color-rule);
  }

  .structured-data > div {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(6.2rem, 0.38fr) minmax(0, 1fr);
    gap: 0.65rem;
    padding: 0.58rem 0.65rem;
    background: var(--color-surface);
  }

  dt {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    line-height: 1.45;
  }

  dd {
    overflow-wrap: anywhere;
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.66rem;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  .structured-data__empty dd {
    color: var(--color-ink-muted);
    font-style: italic;
  }

  .structured-data--compact > div {
    grid-template-columns: minmax(5.2rem, 0.32fr) minmax(0, 1fr);
    padding: 0.42rem 0.5rem;
  }

  .structured-data__empty-message {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }
</style>
