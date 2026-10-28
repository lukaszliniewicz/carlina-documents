<script>
  export let bubble = null;
  export let guestMode = false;
  export let busy = false;
  export let status = "";
  export let bubbleElement = null;
  export let onClose = () => {};
  export let onFieldChange = () => {};
  export let onSave = () => {};
  export let onEdit = () => {};
  export let onDelete = () => {};
</script>

{#if bubble}
  <div
    class="glossary-bubble"
    style={`left:${bubble.left}px;top:${bubble.top}px`}
    role="dialog"
    aria-label="Glossary quick editor"
    bind:this={bubbleElement}
  >
    <header class="glossary-bubble-header">
      <p>Glossary Term</p>
      <button class="btn btn-ghost bubble-close" type="button" on:click={onClose}>Close</button>
    </header>

    <div class="glossary-bubble-body">
      <p class="bubble-source">{bubble.source_term}</p>

      {#if guestMode}
        <p class="bubble-readonly">Target: <strong>{bubble.target_term || "Not set"}</strong></p>
        {#if bubble.context_notes}<p class="bubble-readonly">{bubble.context_notes}</p>{/if}
        <p class="muted">Guest mode: glossary editing is disabled.</p>
      {:else}
        <label>
          Target Term
          <input value={bubble.target_term || ""} on:input={(event) => onFieldChange("target_term", event.currentTarget.value)} />
        </label>
        <label>
          Notes
          <textarea rows="3" value={bubble.context_notes || ""} on:input={(event) => onFieldChange("context_notes", event.currentTarget.value)}></textarea>
        </label>
        <label class="checkbox">
          <input type="checkbox" checked={bubble.needs_review} on:change={(event) => onFieldChange("needs_review", event.currentTarget.checked)} />
          Mark for review
        </label>

        {#if status}<p class={`bubble-status ${status === "Saved" ? "ok" : "error"}`}>{status}</p>{/if}

        <div class="glossary-bubble-actions">
          <button class="btn primary" type="button" disabled={busy} on:click={onSave}>{busy ? "Saving..." : "Save"}</button>
          <button class="btn btn-ghost" type="button" disabled={busy} on:click={onEdit}>Full Editor</button>
          <button class="btn danger" type="button" disabled={busy} on:click={onDelete}>Delete</button>
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .glossary-bubble { position:fixed; z-index:2050; width:min(360px,calc(100vw - 1.3rem)); max-height:min(70vh,420px); overflow:auto; border:1px solid var(--color-rule); border-radius:.66rem; background:var(--color-surface); box-shadow:0 14px 32px rgba(5,6,8,.16); }
  .glossary-bubble-header { display:flex; justify-content:space-between; align-items:center; gap:.75rem; border-bottom:1px solid var(--color-rule); padding:.56rem .62rem; background:var(--color-paper-muted); }
  .glossary-bubble-header p { margin:0; font-size:.75rem; text-transform:uppercase; letter-spacing:.08em; color:var(--color-ink-muted); font-weight:700; }
  .bubble-close { padding:.24rem .54rem; font-size:.75rem; }
  .glossary-bubble-body { display:flex; flex-direction:column; gap:.55rem; padding:.65rem; }
  .bubble-source { margin:0; font-size:1rem; font-weight:700; line-height:1.3; color:var(--color-ink); }
  .bubble-readonly { margin:0; font-size:.86rem; color:var(--color-ink-soft); line-height:1.45; }
  .bubble-status { margin:0; padding:.35rem .46rem; border:1px solid var(--color-rule); border-radius:.45rem; font-size:.81rem; }
  .bubble-status.ok { border-color:color-mix(in srgb,var(--color-active) 30%,var(--color-rule)); background:var(--color-active-muted); color:var(--color-active); }
  .bubble-status.error { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); }
  .glossary-bubble-actions { display:flex; gap:.42rem; flex-wrap:wrap; }
  label { display:flex; flex-direction:column; gap:.35rem; font-size:.9rem; color:var(--color-ink-soft); }
  label.checkbox { flex-direction:row; align-items:center; gap:.5rem; }
  input:not([type="checkbox"]):not([type="radio"]), textarea { width:100%; box-sizing:border-box; border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); padding:.56rem .62rem; font:inherit; border-radius:.5rem; }
  input:not([type="checkbox"]):not([type="radio"]):focus, textarea:focus { outline:2px solid color-mix(in srgb,var(--color-active) 20%,transparent); border-color:color-mix(in srgb,var(--color-active) 38%,var(--color-rule)); outline-offset:1px; }
  .btn { border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); padding:.46rem .76rem; cursor:pointer; font-size:.86rem; border-radius:.48rem; }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .btn.danger { border-color:var(--color-danger); background:var(--color-danger); color:var(--color-surface); }
  .btn.btn-ghost { background:transparent; color:var(--color-ink-muted); }
  .muted { margin:0; font-size:.84rem; color:var(--color-ink-muted); }
</style>
