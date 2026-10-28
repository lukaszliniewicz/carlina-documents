<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let grants = [];
  export let busy = false;
  export let error = "";
  export let createdLink = "";
  export let onClose = () => {};
  export let onCreate = () => {};
  export let onRevoke = () => {};

  let label = "";
  let role = "reviewer";
  let expiresAt = "";
  let copied = false;

  function submit(event) {
    event.preventDefault();
    if (busy || !label.trim()) return;
    onCreate({
      label: label.trim(),
      role,
      expires_at: expiresAt ? new Date(expiresAt).toISOString() : null
    });
  }

  async function copyLink() {
    if (!createdLink) return;
    await navigator.clipboard?.writeText(createdLink);
    copied = true;
  }

  $: if (!open) {
    copied = false;
  }
</script>

<Dialog
  {open}
  title="Share this translation"
  description="Let someone read or review this translation without an account. You can revoke their access at any time."
  onclose={() => !busy && onClose()}
  size="lg"
>
  <div class="share-shell">
    {#if error}<div class="alert error" role="alert">{error}</div>{/if}

    {#if createdLink}
      <section class="issued-link" aria-labelledby="issued-link-title">
        <div>
          <p class="eyebrow">Copy this now</p>
          <h3 id="issued-link-title">Your new share link</h3>
          <p>Anyone with this link can use its assigned permission. Copy it now; it cannot be shown again after you close this dialog.</p>
        </div>
        <div class="copy-row">
          <input aria-label="New share link" readonly value={createdLink} on:focus={(event) => event.currentTarget.select()} />
          <button type="button" class="btn primary" on:click={copyLink}>{copied ? "Copied" : "Copy link"}</button>
        </div>
      </section>
    {/if}

    <form class="grant-form" on:submit={submit}>
      <div class="section-heading">
        <div><h3>Create a link</h3><p>Viewer can read. Reviewer can correct translations, manage reader notes, propose glossary changes, and request—never run—new model work.</p></div>
      </div>
      <div class="form-grid">
        <label>
          Label
          <input bind:value={label} maxlength="128" required placeholder="e.g. Daniel — study review" disabled={busy} />
        </label>
        <label>
          Permission
          <select bind:value={role} disabled={busy}>
            <option value="reviewer">Reviewer</option>
            <option value="viewer">Viewer</option>
          </select>
        </label>
        <label>
          Expires <small>Optional</small>
          <input type="datetime-local" bind:value={expiresAt} disabled={busy} />
        </label>
      </div>
      <button type="submit" class="btn primary create-button" disabled={busy || !label.trim()}>{busy ? "Issuing…" : "Create revocable link"}</button>
    </form>

    <section class="grant-list" aria-labelledby="active-grants-title">
      <div class="section-heading">
        <div><h3 id="active-grants-title">Issued links</h3><p>Revocation ends future access and invalidates its short-lived sessions.</p></div>
        <span>{grants.length}</span>
      </div>
      {#if grants.length}
        <div class="rows">
          {#each grants as grant (grant.id)}
            <article class:revoked={grant.status !== "active"} class="grant-row">
              <div>
                <div class="grant-title"><strong>{grant.label}</strong><span>{grant.role}</span></div>
                <p>
                  Created {grant.created_at ? new Date(grant.created_at).toLocaleString() : "recently"}
                  {#if grant.expires_at} · expires {new Date(grant.expires_at).toLocaleString()}{/if}
                  {#if grant.last_used_at} · last used {new Date(grant.last_used_at).toLocaleString()}{/if}
                </p>
              </div>
              {#if grant.status === "active"}
                <button type="button" class="revoke-button" disabled={busy} on:click={() => onRevoke(grant)}>Revoke</button>
              {:else}
                <span class="revoked-label">Revoked</span>
              {/if}
            </article>
          {/each}
        </div>
      {:else}
        <p class="empty">No share links have been created.</p>
      {/if}
    </section>
  </div>
</Dialog>

<style>
  .share-shell { display:grid; gap:1rem; }
  .alert { padding:.65rem .75rem; border:1px solid transparent; font-size:.82rem; }
  .alert.error { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); }
  .issued-link { display:grid; gap:.75rem; padding:.85rem; border:1px solid color-mix(in srgb,var(--color-clay) 42%,var(--color-rule)); background:var(--color-clay-muted); }
  .eyebrow { margin:0; color:var(--color-clay); font-size:.62rem; font-weight:800; letter-spacing:.13em; text-transform:uppercase; }
  h3 { margin:.12rem 0 0; color:var(--color-ink); font-family:var(--font-editorial,Georgia,serif); font-size:1rem; font-weight:550; }
  p { margin:.22rem 0 0; color:var(--color-ink-muted); font-size:.78rem; line-height:1.48; }
  .copy-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:.45rem; }
  .copy-row input { min-width:0; font-family:var(--font-mono,ui-monospace,monospace); font-size:.72rem; }
  .grant-form, .grant-list { display:grid; gap:.75rem; padding:.85rem; border:1px solid var(--color-rule); background:var(--color-surface); }
  .section-heading { display:flex; justify-content:space-between; gap:1rem; align-items:start; }
  .section-heading > span { color:var(--color-clay); font-size:.74rem; font-weight:750; }
  .form-grid { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(8rem,.65fr) minmax(11rem,.85fr); gap:.65rem; }
  label { display:grid; gap:.3rem; color:var(--color-ink-soft); font-size:.77rem; font-weight:650; }
  label small { color:var(--color-ink-muted); font-weight:400; }
  input, select { width:100%; box-sizing:border-box; min-height:2.3rem; border:1px solid var(--color-rule-strong); padding:.5rem .58rem; background:var(--color-surface); color:var(--color-ink); font:inherit; }
  .btn { min-height:2.2rem; border:1px solid var(--color-rule); padding:.48rem .7rem; background:var(--color-surface); color:var(--color-ink); cursor:pointer; font:inherit; font-size:.76rem; font-weight:650; }
  .btn.primary { border-color:var(--color-clay); background:var(--color-clay); color:var(--color-surface); }
  .btn:disabled, .revoke-button:disabled { opacity:.55; cursor:not-allowed; }
  .create-button { justify-self:end; }
  .rows { display:grid; border-top:1px solid var(--color-rule); }
  .grant-row { display:flex; justify-content:space-between; align-items:start; gap:1rem; padding:.7rem 0; border-bottom:1px solid var(--color-rule); }
  .grant-row.revoked { opacity:.62; }
  .grant-title { display:flex; align-items:baseline; flex-wrap:wrap; gap:.5rem; }
  .grant-title span, .revoked-label { color:var(--color-ink-muted); font-size:.62rem; font-weight:750; letter-spacing:.08em; text-transform:uppercase; }
  .revoke-button { border:0; border-bottom:1px solid currentColor; padding:.2rem; background:transparent; color:var(--color-danger); cursor:pointer; font:inherit; font-size:.72rem; font-weight:700; }
  .empty { margin:0; padding:.8rem; border:1px dashed var(--color-rule); text-align:center; }
  @media (max-width:760px) { .form-grid { grid-template-columns:1fr; } .copy-row { grid-template-columns:1fr; } .create-button { justify-self:stretch; } }
</style>
