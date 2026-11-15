<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let loading = false;
  export let loadingMore = false;
  export let error = "";
  export let payload = null;
  export let stateFilter = "open";
  export let block = null;
  export let canEdit = false;
  export let busyId = "";
  export let onclose = () => {};
  export let onstate = () => {};
  export let onclearblock = () => {};
  export let onnavigate = () => {};
  export let onloadmore = () => {};
  export let ontransition = async () => false;
  export let oncreate = async () => false;

  const KINDS = [
    ["assumption", "Assumption", "A reading or decision the text does not settle."],
    ["source_defect", "Source problem", "A misprint, gap or error in the source."],
    ["unverified", "Unverified", "A name, date, quotation or fact not yet checked."],
    ["departure", "Departure", "A deliberate change from the source's form or content."],
    ["revisit", "Revisit", "Something to come back to once more of the text is done."]
  ];
  const KIND_LABELS = Object.fromEntries(KINDS.map(([kind, label]) => [kind, label]));
  const ORIGIN_LABELS = {
    agent: "Raised by an agent",
    human: "Raised in review",
    translation: "Raised while translating"
  };
  const ACTION_LABELS = { resolve: "Resolved", dismiss: "Dismissed", reopen: "Reopened" };

  let pending = null;
  let note = "";
  let newKind = "assumption";
  let newBody = "";

  $: items = Array.isArray(payload?.items) ? payload.items : [];
  $: summary = payload?.summary || {};
  $: total = payload?.pagination?.total ?? items.length;
  $: busy = Boolean(busyId);

  function startTransition(flag, action) {
    pending = { id: flag.id, action };
    note = "";
  }

  async function confirmTransition(flag) {
    if (!pending || (pending.action !== "reopen" && !note.trim())) return;
    const saved = await ontransition(flag, pending.action, note.trim());
    if (saved) pending = null;
  }

  async function submitFlag(event) {
    event.preventDefault();
    if (!newBody.trim()) return;
    const saved = await oncreate({ kind: newKind, body: newBody.trim() });
    if (saved) newBody = "";
  }

  function when(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
  }
</script>

<Dialog
  {open}
  title="Translator flags"
  description="Assumptions, source problems, unchecked facts, deliberate departures and points to revisit, raised while translating or reviewing. Flags never appear in exported text."
  onclose={() => !busy && onclose()}
  size="lg"
>
  <div class="flags-shell">
    <div class="flags-toolbar">
      <div class="flags-summary" aria-live="polite">
        {#if payload}
          <span><strong>{summary.open || 0}</strong> open</span>
          <span>{summary.resolved || 0} resolved</span>
          <span>{summary.dismissed || 0} dismissed</span>
        {:else}
          <span>Not loaded yet.</span>
        {/if}
      </div>
      <label>Show
        <select value={stateFilter} disabled={loading || busy} on:change={(event) => onstate(event.currentTarget.value)}>
          <option value="open">Open flags</option>
          <option value="resolved">Resolved</option>
          <option value="dismissed">Dismissed</option>
          <option value="all">All flags</option>
        </select>
      </label>
    </div>

    {#if block}
      <div class="flags-scope">
        <span>Block {block.order}</span>
        <button type="button" class="link" disabled={loading || busy} on:click={onclearblock}>Show every block</button>
      </div>
      {#if canEdit}
        <form class="flag-form" on:submit={submitFlag}>
          <label>Kind
            <select bind:value={newKind} disabled={busy}>
              {#each KINDS as [kind, label]}<option value={kind}>{label}</option>{/each}
            </select>
          </label>
          <p class="hint">{KINDS.find(([kind]) => kind === newKind)?.[2]}</p>
          <label>What should the next reader know?
            <textarea bind:value={newBody} rows="3" maxlength="1000" disabled={busy} placeholder="State the doubt and what would settle it."></textarea>
          </label>
          <div class="actions">
            <button type="submit" class="btn primary" disabled={busy || !newBody.trim()}>{busyId === "new" ? "Adding…" : "Add flag"}</button>
          </div>
        </form>
      {/if}
    {/if}

    {#if error}<div class="alert error" role="alert">{error}</div>{/if}

    {#if loading && !payload}
      <p class="empty">Loading flags…</p>
    {:else if payload && !items.length}
      <p class="empty">{stateFilter === "open" ? "No open flags." : "No flags match this view."}</p>
    {:else}
      <ol>
        {#each items as flag (flag.id)}
          <li class={`flag state-${flag.state}`}>
            <header>
              <div>
                <p class="eyebrow">{KIND_LABELS[flag.kind] || flag.kind} · {ORIGIN_LABELS[flag.origin] || flag.origin}</p>
                <button type="button" class="link" on:click={() => onnavigate(flag)}>Block {flag.order}</button>
              </div>
              {#if flag.state !== "open"}<span class="state">{flag.state}</span>{/if}
            </header>
            <p class="body">{flag.body}</p>
            {#if flag.excerpt}<q>{flag.excerpt}</q>{/if}
            {#if flag.history?.length}
              <ul class="history">
                {#each flag.history as event (event.sequence)}
                  <li>{ACTION_LABELS[event.action] || event.action}{event.at ? ` ${when(event.at)}` : ""}{event.note ? `: ${event.note}` : ""}</li>
                {/each}
              </ul>
            {/if}
            {#if canEdit}
              {#if pending?.id === flag.id}
                <div class="transition">
                  {#if pending.action !== "reopen"}
                    <label>{pending.action === "resolve" ? "How was it settled?" : "Why does it not need action?"}
                      <textarea bind:value={note} rows="2" maxlength="1000" disabled={busy}></textarea>
                    </label>
                  {/if}
                  <div class="actions">
                    <button type="button" class="btn" disabled={busy} on:click={() => (pending = null)}>Cancel</button>
                    <button type="button" class="btn primary" disabled={busy || (pending.action !== "reopen" && !note.trim())} on:click={() => confirmTransition(flag)}>
                      {busyId === flag.id ? "Saving…" : pending.action === "resolve" ? "Resolve flag" : pending.action === "dismiss" ? "Dismiss flag" : "Reopen flag"}
                    </button>
                  </div>
                </div>
              {:else if flag.state === "open"}
                <div class="actions">
                  <button type="button" class="btn" disabled={busy} on:click={() => startTransition(flag, "dismiss")}>Dismiss…</button>
                  <button type="button" class="btn secondary" disabled={busy} on:click={() => startTransition(flag, "resolve")}>Resolve…</button>
                </div>
              {:else}
                <div class="actions">
                  <button type="button" class="btn" disabled={busy} on:click={() => startTransition(flag, "reopen")}>Reopen</button>
                </div>
              {/if}
            {/if}
          </li>
        {/each}
      </ol>
      {#if payload?.pagination?.has_more}
        <button type="button" class="btn load-more" disabled={loadingMore} on:click={onloadmore}>
          {loadingMore ? "Loading more…" : `Load more (${items.length} of ${total})`}
        </button>
      {/if}
    {/if}
  </div>
</Dialog>

<style>
  .flags-shell { display:grid; gap:.8rem; }
  .flags-toolbar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.8rem; }
  .flags-summary { display:flex; flex-wrap:wrap; gap:.45rem; color:var(--color-ink-soft); font-size:.82rem; }
  .flags-summary span { border:1px solid var(--color-rule); background:var(--color-paper-muted); border-radius:999px; padding:.2rem .48rem; }
  .flags-scope { display:flex; align-items:center; gap:.6rem; font-size:.82rem; color:var(--color-ink-soft); font-weight:650; }
  .flag-form, .transition { display:grid; gap:.45rem; padding:.7rem; border:1px solid var(--color-rule); background:var(--color-paper-muted); }
  label { display:grid; gap:.3rem; color:var(--color-ink-soft); font-size:.74rem; font-weight:650; }
  .flags-toolbar label { display:flex; align-items:center; gap:.4rem; }
  select, textarea { box-sizing:border-box; border:1px solid var(--color-rule-strong); padding:.45rem .55rem; background:var(--color-surface); color:var(--color-ink); font:inherit; font-size:.82rem; }
  textarea { width:100%; resize:vertical; }
  .hint { margin:0; color:var(--color-ink-muted); font-size:.74rem; }
  .alert.error { padding:.65rem .75rem; border:1px solid color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); font-size:.82rem; }
  .empty { padding:1.1rem; border:1px dashed var(--color-rule); color:var(--color-ink-muted); text-align:center; margin:0; }
  ol { display:flex; flex-direction:column; gap:.6rem; margin:0; padding:0 .2rem 0 0; list-style:none; max-height:min(60vh,44rem); overflow:auto; }
  .flag { display:grid; gap:.45rem; padding:.72rem .78rem; border:1px solid var(--color-rule); border-left:4px solid var(--color-clay); background:var(--color-surface); }
  .flag.state-resolved, .flag.state-dismissed { border-left-color:var(--color-rule-strong); opacity:.78; }
  header { display:flex; justify-content:space-between; align-items:start; gap:1rem; }
  .eyebrow { margin:0 0 .12rem; color:var(--color-clay); font-size:.61rem; font-weight:800; letter-spacing:.1em; text-transform:uppercase; }
  .link { border:0; padding:0; background:transparent; color:var(--color-active); cursor:pointer; font:inherit; font-size:.8rem; font-weight:650; }
  .link:disabled { opacity:.55; cursor:not-allowed; }
  .state { padding:.2rem .38rem; border:1px solid var(--color-rule); color:var(--color-ink-muted); font-size:.6rem; font-weight:800; letter-spacing:.07em; text-transform:uppercase; }
  .body { margin:0; color:var(--color-ink); font-size:.86rem; line-height:1.5; white-space:pre-wrap; overflow-wrap:anywhere; }
  q { display:block; color:var(--color-ink-soft); font-size:.8rem; font-style:italic; overflow-wrap:anywhere; }
  .history { margin:0; padding-left:1rem; color:var(--color-ink-muted); font-size:.74rem; }
  .actions { display:flex; justify-content:flex-end; flex-wrap:wrap; gap:.45rem; }
  .btn { min-height:2.1rem; border:1px solid var(--color-rule); padding:.42rem .7rem; background:var(--color-surface); color:var(--color-ink); cursor:pointer; font:inherit; font-size:.75rem; font-weight:650; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .btn.secondary { border-color:var(--color-umber); color:var(--color-umber); }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .load-more { justify-self:center; color:var(--color-active); }
</style>
