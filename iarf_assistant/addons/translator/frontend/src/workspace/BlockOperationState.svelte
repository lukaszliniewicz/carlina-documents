<script>
  export let operation = null;
  export let canStop = false;
  export let stopBusy = false;
  export let onStop = () => {};

  $: state = String(operation?.state || "").toLowerCase();
  $: stopping = ["cancel_requested", "cancelling"].includes(state);
  $: active = ["queued", "running", "executing", "started", "pending", "waiting_external", "finalizing", "cancel_requested", "cancelling"].includes(state);
  $: partial = state === "partial";
  $: tone = partial ? "partial" : ["failed", "error", "cancelled"].includes(state) ? "error" : active ? "busy" : "ok";
  $: failure = operation?.block_failure || null;
  $: recoveryGuidance = {
    translate_remaining: "Start a new translation for the remaining blocks; completed windows are preserved.",
    rebase_required: "The source or dependencies changed. Rebase the workspace before translating again.",
    manual_review: "Provider delivery could not be proven safe to replay. Review the affected window before continuing.",
    support_required: "A validated response could not be activated safely. Keep the completed windows and inspect the affected window.",
  }[failure?.recovery_action] || "Review the affected window; completed windows are preserved.";
</script>

{#if operation && (operation.id || operation.state)}
  <section class={`operation-state ${tone}`} aria-live="polite" data-testid="block-operation-state">
    <div>
      <strong>{stopping ? "Stopping block workflow" : active ? "Block workflow in progress" : partial ? "Block workflow partially complete" : `Workflow ${state || "updated"}`}</strong>
      {#if operation.message}<span>{operation.message}</span>{/if}
      {#if partial}
        <span class="partial-guidance" data-testid="block-operation-guidance">{recoveryGuidance}</span>
      {/if}
    </div>
    <div class="operation-actions">
      {#if operation.progress != null}
        <progress max="100" value={Math.max(0, Math.min(100, Number(operation.progress) || 0))}>
          {operation.progress}%
        </progress>
      {/if}
      {#if active && canStop}
        <button
          type="button"
          class="stop-button"
          disabled={stopBusy || stopping}
          title="Prevent another window from starting. A provider request already in flight may finish before the workflow stops."
          on:click={onStop}
        >
          {stopBusy || stopping ? "Stopping..." : "Stop workflow"}
        </button>
      {/if}
    </div>
  </section>
{/if}

<style>
  .operation-state { display:flex; align-items:center; justify-content:space-between; gap:.8rem; padding:.8rem 1rem; border:1px solid var(--color-rule,#d0c8bc); background:var(--color-surface,#faf8f3); color:var(--color-ink-soft,#4d4842); }
  .operation-state > div { display:grid; gap:.15rem; min-width:0; }
  .operation-state > .operation-actions { display:flex; align-items:center; justify-content:end; gap:.55rem; }
  .operation-state span { color:var(--color-ink-muted,#667); font-size:.82rem; overflow-wrap:anywhere; }
  .operation-state.busy { border-color:color-mix(in srgb,var(--color-tobacco,#69583f) 42%,var(--color-rule,#d0c8bc)); background:var(--color-tobacco-muted,#e4ded2); }
  .operation-state.partial { border-color:color-mix(in srgb,var(--color-tobacco,#69583f) 48%,var(--color-rule,#d0c8bc)); color:var(--color-tobacco,#69583f); background:var(--color-tobacco-muted,#e4ded2); }
  .operation-state.error { border-color:color-mix(in srgb,var(--color-danger,#704745) 42%,var(--color-rule,#d0c8bc)); color:var(--color-danger,#704745); background:var(--color-danger-muted,#e8d9d7); }
  .partial-guidance { color:inherit !important; }
  progress { width:9rem; accent-color:var(--color-umber,#5c5149); }
  .stop-button { min-height:2.05rem; border:1px solid color-mix(in srgb,var(--color-umber,#5c5149) 55%,var(--color-rule,#d0c8bc)); border-radius:.4rem; padding:.42rem .65rem; background:var(--color-surface,#faf8f3); color:var(--color-umber,#5c5149); cursor:pointer; font:inherit; font-size:.73rem; font-weight:650; letter-spacing:.02em; white-space:nowrap; }
  .stop-button:disabled { opacity:.58; cursor:not-allowed; }
  @media (max-width: 600px) { .operation-state { align-items:start; flex-direction:column; } .operation-state > .operation-actions { width:100%; align-items:stretch; flex-direction:column; } progress { width:100%; } .stop-button { width:100%; } }
</style>
