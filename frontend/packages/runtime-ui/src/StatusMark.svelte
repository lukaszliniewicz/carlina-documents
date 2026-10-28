<script lang="ts">
  interface Props {
    status: string;
    label?: string;
    compact?: boolean;
  }

  let { status, label, compact = false }: Props = $props();

  const normalized = $derived(status.toLowerCase());
  const tone = $derived(
    normalized === "completed" || normalized === "approved"
      ? "success"
      : normalized === "failed" ||
          normalized === "cancelled" ||
          normalized === "rejected"
        ? "danger"
        : normalized.includes("waiting") ||
            normalized === "paused" ||
            normalized === "changes_requested"
          ? "warning"
          : normalized === "running"
            ? "active"
            : "neutral",
  );
  const displayLabel = $derived(
    label ??
      status
        .replace(/[_-]+/g, " ")
        .replace(/^\w/, (value) => value.toUpperCase()),
  );
</script>

<span
  class="status-mark"
  class:status-mark--compact={compact}
  class:status-mark--success={tone === "success"}
  class:status-mark--danger={tone === "danger"}
  class:status-mark--warning={tone === "warning"}
  class:status-mark--active={tone === "active"}
>
  <i aria-hidden="true"></i>
  {displayLabel}
</span>

<style>
  .status-mark {
    min-height: 1.55rem;
    display: inline-flex;
    align-items: center;
    gap: 0.38rem;
    border: 1px solid var(--color-rule);
    padding: 0.2rem 0.48rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-soft);
    font-size: 0.63rem;
    font-weight: 720;
    letter-spacing: 0.055em;
    line-height: 1;
    text-transform: uppercase;
  }

  i {
    width: 0.38rem;
    height: 0.38rem;
    border: 1px solid currentColor;
    background: transparent;
  }

  .status-mark--compact {
    min-height: auto;
    border: 0;
    padding: 0;
    background: transparent;
    font-size: 0.58rem;
  }

  .status-mark--success {
    border-color: color-mix(in srgb, var(--color-lichen) 38%, var(--color-rule));
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .status-mark--success i,
  .status-mark--active i {
    background: currentColor;
  }

  .status-mark--warning {
    border-color: color-mix(in srgb, var(--color-tobacco) 38%, var(--color-rule));
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
  }

  .status-mark--danger {
    border-color: color-mix(in srgb, var(--color-danger) 38%, var(--color-rule));
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .status-mark--active {
    border-color: color-mix(in srgb, var(--color-focus) 35%, var(--color-rule));
    background: color-mix(in srgb, var(--color-focus) 10%, var(--color-surface));
    color: var(--color-focus);
  }
</style>
