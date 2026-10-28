<script>
  export let entries = [];
  export let proposals = [];
  export let suggestions = [];
  export let busy = false;
  export let loaded = false;
  export let error = "";
  export let readOnly = false;
  export let accessRole = readOnly ? "viewer" : "owner";
  export let onSave = async () => false;
  export let onSuggest = async () => false;
  export let onResolveSuggestion = async () => false;
  export let focusConceptId = "";
  let editing = null;
  let focusedConceptId = "";

  $: canLock = !readOnly && accessRole === "owner";
  $: canSuggest = !readOnly && accessRole === "reviewer";

  $: if (canLock && loaded && focusConceptId && focusConceptId !== focusedConceptId) {
    const entry = entries.find((item) => item.concept_id === focusConceptId);
    if (entry) {
      focusedConceptId = focusConceptId;
      beginEdit(entry);
    }
  }

  $: if (editing?.concept_id) {
    if (editing.proposal_id) {
      const current = proposals.find((proposal) => proposal.proposal_id === editing.proposal_id);
      if (!current) editing = null;
    } else {
      const current = entries.find((entry) => entry.concept_id === editing.concept_id);
      if (current && current.active_version !== editing.active_version) editing = null;
    }
  }

  function beginNew() {
    editing = {
      concept_id: "",
      proposal_id: "",
      active_version: 0,
      source_term: "",
      target_term: "",
      accepted_target_forms_text: "",
      notes: ""
    };
  }

  function beginSuggestion(suggestion) {
    editing = {
      concept_id: "",
      proposal_id: "",
      suggestion_id: suggestion.id,
      version: suggestion.version,
      active_version: 0,
      source_term: suggestion.source_term,
      target_term: suggestion.target_term,
      accepted_target_forms_text: "",
      notes: suggestion.notes || ""
    };
  }

  function beginEdit(entry) {
    editing = {
      concept_id: entry.concept_id,
      proposal_id: "",
      active_version: entry.active_version,
      source_term: entry.source_term,
      target_term: entry.target_term,
      accepted_target_forms_text: (entry.usage?.accepted_target_forms || []).join("\n"),
      notes: entry.notes || ""
    };
  }

  function beginProposal(proposal) {
    editing = {
      concept_id: proposal.concept_id,
      proposal_id: proposal.proposal_id,
      active_version: 0,
      source_term: proposal.source_term,
      target_term: proposal.target_term,
      accepted_target_forms_text: (proposal.usage?.accepted_target_forms || []).join("\n"),
      notes: proposal.notes || ""
    };
  }

  async function submit(event) {
    event.preventDefault();
    if (busy || (!canLock && !canSuggest) || !editing) return;
    const payload = {
      ...editing,
      source_term: editing.source_term.trim(),
      target_term: editing.target_term.trim(),
      accepted_target_forms: canLock
        ? String(editing.accepted_target_forms_text || "")
            .split(/\r?\n/)
            .map((item) => item.trim())
            .filter(Boolean)
        : [],
      notes: editing.notes.trim() || null
    };
    const saved = editing.suggestion_id
      ? await onResolveSuggestion(editing.suggestion_id, "approve", payload)
      : canLock
        ? await onSave(payload)
        : await onSuggest(payload);
    if (saved) editing = null;
  }
</script>

<section class="glossary-panel" aria-label="Document terminology">
      <p class="panel-meta" role="status">
        {#if loaded}
          {entries.length} locked{#if proposals.length + suggestions.length} · {proposals.length + suggestions.length} suggested{/if}
        {:else}
          {busy ? "Loading terminology…" : "Terminology has not loaded yet."}
        {/if}
      </p>
      <div class="panel-body">
      <div class="panel-intro">
        <p>Locked terms guide the next translation run. Analysis and translation can add suggestions below. They guide later batches as advice; approval makes them authoritative for the next run.</p>
        {#if canLock || canSuggest}
          <button type="button" class="btn" disabled={busy || !loaded} on:click={beginNew}>{canLock ? "Add term" : "Suggest term"}</button>
        {/if}
      </div>

      {#if error}<p class="panel-error" role="alert">{error}</p>{/if}

      {#if editing && (canLock || canSuggest)}
        <form class="glossary-form" on:submit={submit}>
          <div class="form-heading">
            <strong>{editing.suggestion_id ? "Review collaborator suggestion" : editing.proposal_id ? "Review analysis suggestion" : editing.concept_id ? "Edit locked term" : canLock ? "Add glossary term" : "Suggest glossary term"}</strong>
            {#if editing.proposal_id}<span>Change anything the analysis got wrong before locking it.</span>{/if}
            {#if editing.suggestion_id}<span>Change anything necessary before promoting this suggestion into the locked glossary.</span>{/if}
            {#if canSuggest}<span>The owner will review this proposal before it can guide model work.</span>{/if}
          </div>
          <label>
            <span>Source term</span>
            <input bind:value={editing.source_term} required maxlength="4096" disabled={busy} />
          </label>
          <label>
            <span>Preferred translation</span>
            <input bind:value={editing.target_term} required maxlength="4096" disabled={busy} />
          </label>
          {#if canLock}
            <label class="variants-field">
              <span>Accepted contextual variants <small>Optional; one rendering per line</small></span>
              <textarea bind:value={editing.accepted_target_forms_text} rows="2" maxlength="16384" disabled={busy}></textarea>
            </label>
          {/if}
          <label class="notes-field">
            <span>Notes <small>Optional usage, register, or context guidance</small></span>
            <textarea bind:value={editing.notes} rows="3" maxlength="32768" disabled={busy}></textarea>
          </label>
          <div class="form-actions">
            <button type="button" class="btn secondary" disabled={busy} on:click={() => (editing = null)}>Cancel</button>
            <button type="submit" class="btn primary" disabled={busy || !editing.source_term.trim() || !editing.target_term.trim()}>{busy ? "Saving…" : editing.suggestion_id || editing.proposal_id ? "Approve for next run" : canLock ? "Save for next run" : "Send suggestion"}</button>
          </div>
        </form>
      {/if}

      {#if suggestions.length}
        <section class="suggestions" aria-labelledby="collaborator-suggestions-title">
          <div class="section-heading">
            <div>
              <strong id="collaborator-suggestions-title">Review suggestions</strong>
              <p>Durable proposals from reviewers or imported research; never active terminology by themselves.</p>
            </div>
            <span>{suggestions.length}</span>
          </div>
          <div class="term-list suggestion-list">
            {#each suggestions as suggestion (suggestion.id)}
              <article class="term-row suggestion-row">
                <div class="term-copy">
                  <p><strong>{suggestion.source_term}</strong><span aria-hidden="true">→</span><strong>{suggestion.target_term}</strong></p>
                  {#if suggestion.notes}<p class="term-notes">{suggestion.notes}</p>{/if}
                  <p class="proposal-by">{suggestion.author?.label || "External research"} · {suggestion.state}</p>
                </div>
                {#if canLock && suggestion.state === "pending"}
                  <div class="suggestion-actions">
                    <button type="button" class="review-button" disabled={busy} on:click={() => beginSuggestion(suggestion)}>Review</button>
                    <button type="button" class="reject-button" disabled={busy} on:click={() => onResolveSuggestion(suggestion.id, "reject", { version: suggestion.version })}>Reject</button>
                  </div>
                {/if}
              </article>
            {/each}
          </div>
        </section>
      {/if}

      {#if proposals.length}
        <section class="suggestions" aria-labelledby="analysis-suggestions-title">
          <div class="section-heading">
            <div>
              <strong id="analysis-suggestions-title">{proposals.some((proposal) => proposal.usage) ? "Working glossary" : "Analysis suggestions"}</strong>
              <p>Useful context now; locked terminology only after your approval.</p>
            </div>
            <span>{proposals.length}</span>
          </div>
          <div class="term-list suggestion-list">
            {#each proposals as proposal (proposal.proposal_id)}
              <article class="term-row suggestion-row">
                <div class="term-copy">
                  <p><strong>{proposal.source_term}</strong><span aria-hidden="true">→</span><strong>{proposal.target_term}</strong></p>
                  {#if proposal.notes}<p class="term-notes">{proposal.notes}</p>{/if}
                  {#if proposal.usage}
                    <p class="term-notes">{proposal.usage.sense || "General sense"} · {proposal.usage.match_policy === "exact" ? "Fixed spelling" : "Inflect grammatically"}</p>
                    <p class="term-notes">Source forms: {proposal.usage.source_forms.join(", ")}</p>
                    {#if proposal.usage.accepted_target_forms?.length}
                      <p class="term-notes">Accepted target variants: {proposal.usage.accepted_target_forms.join(", ")}</p>
                    {/if}
                  {/if}
                </div>
                {#if canLock}
                  <button type="button" class="review-button" disabled={busy} on:click={() => beginProposal(proposal)}>Review</button>
                {/if}
              </article>
            {/each}
          </div>
        </section>
      {/if}

      {#if entries.length}
        <section class="locked-terms" aria-labelledby="locked-terms-title">
          <div class="section-heading">
            <strong id="locked-terms-title">Locked terms</strong>
            <span>{entries.length}</span>
          </div>
          <div class="term-list">
            {#each entries as entry (entry.concept_id)}
              <article class="term-row">
                <div class="term-copy">
                  <p><strong>{entry.source_term}</strong><span aria-hidden="true">→</span><strong>{entry.target_term}</strong></p>
                  {#if entry.notes}<p class="term-notes">{entry.notes}</p>{/if}
                  {#if entry.usage}
                    <p class="term-notes">{entry.usage.sense || "General sense"} · {entry.usage.match_policy === "exact" ? "Fixed spelling" : "Inflect grammatically"}</p>
                    <p class="term-notes">Source forms: {entry.usage.source_forms.join(", ")}</p>
                    {#if entry.usage.accepted_target_forms?.length}
                      <p class="term-notes">Accepted target variants: {entry.usage.accepted_target_forms.join(", ")}</p>
                    {/if}
                  {/if}
                </div>
                {#if canLock}
                  <button type="button" class="edit-button" disabled={busy} on:click={() => beginEdit(entry)}>Edit</button>
                {/if}
              </article>
            {/each}
          </div>
        </section>
      {:else if loaded && !editing && !proposals.length && !suggestions.length}
        <p class="empty-state">No locked or suggested terms yet. Add the names, phrases, and stylistic constraints that must remain consistent.</p>
      {/if}
      </div>
</section>

<style>
  .glossary-panel { min-width:0; background:var(--color-surface,#faf8f3); }
  .panel-meta { margin:0 0 .8rem; color:var(--color-clay,#7c6053); font-size:.8rem; font-weight:650; }
  .panel-body { display:grid; gap:1.25rem; }
  .panel-intro { display:flex; justify-content:space-between; align-items:start; gap:1rem; }
  .panel-intro p, .empty-state { max-width:70ch; margin:0; color:var(--color-ink-muted,#675f56); font-size:.8rem; line-height:1.5; }
  .btn { min-height:2.1rem; border:1px solid var(--color-rule,#d0c8bc); border-radius:.4rem; padding:.45rem .68rem; background:transparent; color:var(--color-ink-soft,#4d4842); cursor:pointer; font:inherit; font-size:.73rem; font-weight:650; white-space:nowrap; }
  .btn:disabled, .edit-button:disabled, .review-button:disabled, .reject-button:disabled { opacity:.55; cursor:not-allowed; }
  .glossary-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.7rem; padding:.85rem; background:var(--color-paper-muted,#eee9e0); border:1px solid var(--color-rule,#d0c8bc); }
  .form-heading { grid-column:1/-1; display:grid; gap:.18rem; padding-bottom:.15rem; }
  .form-heading strong { color:var(--color-ink,#302d29); font-family:var(--font-editorial,Georgia,serif); font-size:.94rem; font-weight:500; }
  .form-heading span { color:var(--color-ink-muted,#675f56); font-size:.74rem; }
  label { display:grid; gap:.32rem; color:var(--color-ink-soft,#4d4842); font-size:.72rem; font-weight:700; }
  label small { margin-left:.35rem; color:var(--color-ink-muted,#675f56); font-weight:400; }
  input, textarea { width:100%; box-sizing:border-box; border:1px solid var(--color-rule-strong,#968b7d); padding:.58rem .62rem; background:var(--color-surface,#faf8f3); color:var(--color-ink,#302d29); font:inherit; font-size:.82rem; }
  textarea { resize:vertical; line-height:1.45; }
  .variants-field, .notes-field, .form-actions { grid-column:1/-1; }
  .form-actions { display:flex; justify-content:flex-end; gap:.45rem; }
  .btn.secondary { background:var(--color-surface,#faf8f3); }
  .btn.primary { border-color:var(--color-umber,#5c5149); background:var(--color-umber,#5c5149); color:var(--color-surface,#faf8f3); }
  .panel-error { margin:0; padding:.55rem .65rem; border:1px solid color-mix(in srgb,var(--color-danger,#b33) 35%,var(--color-rule,#ddd)); background:var(--color-danger-muted,#fff0f0); color:var(--color-danger,#b33); font-size:.78rem; }
  .suggestions, .locked-terms { display:grid; gap:.45rem; }
  .section-heading { display:flex; align-items:end; justify-content:space-between; gap:1rem; color:var(--color-ink-soft,#4d4842); }
  .section-heading > div { display:grid; gap:.1rem; }
  .section-heading strong { font-family:var(--font-editorial,Georgia,serif); font-size:.92rem; font-weight:500; }
  .section-heading p { margin:0; color:var(--color-ink-muted,#675f56); font-size:.73rem; }
  .section-heading > span { color:var(--color-clay,#7c6053); font-size:.7rem; font-weight:750; }
  .term-list { display:grid; border-top:1px solid var(--color-rule,#d0c8bc); }
  .suggestion-list { padding:0 .72rem; border:1px solid var(--color-rule,#d0c8bc); background:color-mix(in srgb,var(--color-paper-muted,#eee9e0) 58%,var(--color-surface,#faf8f3)); }
  .term-row { display:flex; justify-content:space-between; align-items:start; gap:1rem; padding:.7rem 0; border-bottom:1px solid var(--color-rule,#d0c8bc); }
  .term-copy { min-width:0; }
  .term-copy p { display:flex; gap:.55rem; align-items:baseline; margin:0; overflow-wrap:anywhere; font-size:.84rem; }
  .term-copy p span { color:var(--color-clay,#7c6053); }
  .term-copy .term-notes { display:block; margin-top:.28rem; color:var(--color-ink-muted,#675f56); font-size:.76rem; line-height:1.45; white-space:pre-wrap; }
  .term-copy .proposal-by { display:block; margin-top:.28rem; color:var(--color-ink-muted,#675f56); font-size:.64rem; letter-spacing:.04em; }
  .edit-button, .review-button { border:0; padding:.2rem; background:transparent; color:var(--color-clay,#7c6053); cursor:pointer; font:inherit; font-size:.72rem; font-weight:700; }
  .review-button { border-bottom:1px solid currentColor; }
  .suggestion-actions { display:flex; align-items:center; gap:.45rem; }
  .reject-button { border:0; padding:.2rem; background:transparent; color:var(--color-danger,#a33); cursor:pointer; font:inherit; font-size:.69rem; }
  @media (max-width:650px) { .panel-intro { flex-direction:column; } .glossary-form { grid-template-columns:1fr; } .form-heading, .variants-field, .notes-field, .form-actions { grid-column:1; } }
</style>
