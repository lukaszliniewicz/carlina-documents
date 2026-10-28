<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    children: Snippet;
    tone?: "neutral" | "success" | "warning" | "danger";
  }

  let { children, tone = "neutral" }: Props = $props();
</script>

<span class:success={tone === "success"} class:warning={tone === "warning"} class:danger={tone === "danger"}>
  {@render children()}
</span>

<style>
  span {
    display: inline-flex;
    align-items: center;
    min-height: 1.45rem;
    border: 1px solid var(--color-rule);
    padding: 0.18rem 0.4rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-soft);
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    line-height: 1;
    text-transform: uppercase;
  }

  .success {
    border-color: color-mix(in srgb, var(--color-lichen) 45%, var(--color-rule));
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .warning {
    border-color: color-mix(in srgb, var(--color-tobacco) 42%, var(--color-rule));
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
  }

  .danger {
    border-color: color-mix(in srgb, var(--color-danger) 42%, var(--color-rule));
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }
</style>
