<script>
  import { onMount, onDestroy } from "svelte";
  import { isAbortError } from "../lib/api.js";
  import { createSharedGlossaryClient, glossaryMutation } from "./shared-glossary-api.js";

  export let client;
  export let projectId;
  export let operationActive = false;
  export let onApplied = async () => {};
  export let onCheckTranslation = () => {};
  export let onReviewLocked = () => {};

  const api = createSharedGlossaryClient(client, projectId);
  const controller = new AbortController();
  let assets = [];
  let state = null;
  let selectedAssetId = "";
  let asset = null;
  let busy = false;
  let error = "";
  let notice = "";
  let name = "";
  let reason = "";
  let selectedTerms = [];
  let comparison = null;
  let choices = {};
  let pending = null;
  let confirmDetach = false;
  let termQuery = "";
  let compareQuery = "";
  let showUnchanged = false;
  let loaded = false;

  $: localTerms = state?.local_terms || [];
  $: visibleTerms = localTerms.filter((term) => `${term.source_term} ${term.target_term} ${term.sense || ""}`.toLocaleLowerCase().includes(termQuery.toLocaleLowerCase()));
  $: visibleRows = (comparison?.rows || []).filter((row) => (showUnchanged || row.status !== "unchanged") && `${row.incoming?.source_term || row.local?.source_term || row.base?.source_term || ""} ${row.incoming?.target_term || ""} ${row.local?.target_term || ""}`.toLocaleLowerCase().includes(compareQuery.toLocaleLowerCase()));
  $: unresolved = (comparison?.rows || []).filter((row) => row.status !== "unchanged" && !choices[row.id]).length;
  $: incomingCount = Object.values(choices).filter((choice) => choice === "incoming").length;

  async function refreshData() {
    const [list, projectState] = await Promise.all([api.list(controller.signal), api.state(controller.signal)]);
    assets = list.items;
    state = projectState;
    loaded = true;
    if (!selectedAssetId && state.binding) selectedAssetId = state.binding.asset_id;
    if (selectedAssetId) asset = await api.asset(selectedAssetId, controller.signal);
    selectedTerms = selectedTerms.filter((id) => projectState.local_terms.some((term) => term.concept_id === id));
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    error = "";
    try { await action(); }
    catch (caught) {
      if (!isAbortError(caught)) error = caught.message || "The glossary action could not be completed. Refresh the comparison and try again.";
    } finally { busy = false; }
  }

  async function refresh() {
    await run(async () => { await refreshData(); comparison = null; });
  }

  async function chooseAsset(event) {
    selectedAssetId = event.currentTarget.value;
    comparison = null;
    choices = {};
    asset = null;
    if (selectedAssetId) await run(async () => { asset = await api.asset(selectedAssetId, controller.signal); });
  }

  async function mutate(action, body, submit, message) {
    await run(async () => {
      pending = glossaryMutation(pending, action, body);
      await submit({ ...body, idempotency_key: pending.key });
      pending = null;
      comparison = null;
      notice = message;
      await refreshData();
      await onApplied();
    });
  }

  async function createAsset() {
    if (!name.trim() || !selectedTerms.length) return;
    await mutate("create", { name: name.trim(), concept_ids: selectedTerms },
      async (body) => {
        const result = await api.create(body, controller.signal);
        selectedAssetId = result.id || result.asset_id;
        name = "";
      }, "Shared glossary created. Link this book to it to review future updates.");
  }

  async function preview(mode) {
    if (!asset) return;
    await run(async () => {
      comparison = await api.preview({ asset_id: selectedAssetId, revision: asset.revision, mode }, controller.signal);
      choices = {};
      compareQuery = "";
      showUnchanged = mode === "copy";
      notice = "";
    });
  }

  function setChoice(id, choice) { choices = { ...choices, [id]: choice }; }
  function selectSuggested() {
    const next = { ...choices };
    for (const row of comparison.rows) {
      if (["add", "update"].includes(row.status) && row.can_take_incoming) next[row.id] = "incoming";
      else if (["local", "retired"].includes(row.status)) next[row.id] = "local";
    }
    choices = next;
  }

  async function applyComparison() {
    if (!comparison) return;
    const body = {
      asset_id: comparison.asset_id, revision: comparison.revision, mode: comparison.mode,
      preview_token: comparison.preview_token,
      decisions: Object.entries(choices).map(([id, choice]) => ({ id, choice })).sort((a, b) => a.id.localeCompare(b.id))
    };
    await mutate("apply", body, (value) => api.apply(value, controller.signal),
      "Terminology saved for the next translation run. Existing translated text is unchanged. Check it below for passages that may need revision.");
  }

  async function detach() {
    await mutate("detach", { expected_version: state.binding?.version || state.binding_version }, (body) => api.detach(body, controller.signal),
      "Glossary unlinked. This book keeps its terms and their origins; future shared updates will not be offered.");
    confirmDetach = false;
  }

  async function contribute() {
    if (!asset || !reason.trim() || !selectedTerms.length) return;
    await mutate(`contribute:${selectedAssetId}`, { expected_revision: asset.revision, concept_ids: selectedTerms, reason: reason.trim(), retire_ids: [] },
      (body) => api.contribute(selectedAssetId, body, controller.signal),
      "Contribution prepared. Review the changes below before publishing them to the shared glossary.");
  }

  async function review(proposal, decision) {
    await mutate(`review:${proposal.id}`, { expected_revision: asset.revision, decision },
      (body) => api.review(selectedAssetId, proposal.id, body, controller.signal),
      decision === "accept" ? "Shared revision published. Linked books keep their adopted versions until their owners review the update." : "Contribution rejected. Shared terminology is unchanged.");
  }

  function toggleTerm(id) {
    selectedTerms = selectedTerms.includes(id) ? selectedTerms.filter((value) => value !== id) : [...selectedTerms, id];
  }

  onMount(refresh);
  onDestroy(() => controller.abort());
</script>

<section class="shared-panel" aria-label="Shared glossaries" aria-busy={busy}>
  <p class="intro">Reuse terminology across volumes, collections, or books by an author. Link a version to review future updates, or copy terms for an independent starting point.</p>
  {#if error}<p class="message error" role="alert">{error}</p>{/if}
  {#if notice}<p class="message" role="status">{notice}</p>{/if}
  {#if operationActive}<p class="message">Translation is running. You can prepare and review contributions now; importing, updating, and unlinking become available when the run finishes.</p>{/if}
  {#if state?.binding}
    <div class="binding">
      <div><strong>{state.binding.asset_name}</strong><p>Using revision {state.binding.adopted_revision}{#if state.binding.latest_revision > state.binding.adopted_revision} · Revision {state.binding.latest_revision} available{/if}</p></div>
      <button type="button" disabled={busy || operationActive} on:click={() => confirmDetach = !confirmDetach}>Unlink…</button>
    </div>
    {#if state.binding.stale_source_plan}<p class="message">This book’s source plan changed. Unlink the historical glossary relationship, then review a fresh link. Existing terms and historical translations are retained.</p>{/if}
    {#if confirmDetach}<div class="message"><p>Keep this book’s terms but stop tracking the shared glossary?</p><div class="actions"><button type="button" disabled={busy} on:click={() => confirmDetach = false}>Cancel</button><button type="button" disabled={busy || operationActive} on:click={detach}>Unlink and keep terms</button></div></div>{/if}
  {:else if loaded}
    <p class="hint">This book has no linked glossary. Its terminology is independent.</p>
    {#if state?.stale_source_plan}<div class="message"><p>This book’s source plan changed. Reset the historical term links before copying or contributing terms again. Terminology and history are retained.</p><button type="button" disabled={busy || operationActive} on:click={detach}>Reset historical term links</button></div>{/if}
  {/if}
  <div class="asset-picker">
    <label>Shared glossary<select bind:value={selectedAssetId} on:change={chooseAsset} disabled={busy}><option value="">Choose a glossary…</option>{#each assets as item (item.id)}<option value={item.id}>{item.name} · {item.source_lang} → {item.target_lang} · revision {item.revision}</option>{/each}</select></label>
    <button type="button" disabled={busy} on:click={refresh}>{busy ? "Working…" : "Refresh"}</button>
  </div>
  {#if asset}
    <div class="actions">
      {#if state?.binding?.asset_id === selectedAssetId}<button type="button" class="primary" disabled={busy || operationActive} on:click={() => preview("update")}>Review updates</button>
      {:else}<button type="button" class="primary" disabled={busy || operationActive || !!state?.binding} on:click={() => preview("link")}>Review and link</button>{/if}
      <button type="button" disabled={busy || operationActive} on:click={() => preview("copy")}>Copy selected terms…</button>
    </div>
    <p class="hint">Imported terms are working advice. Approve them in “This book” to make them authoritative. Human-locked choices are preserved.</p>
    <details><summary>Browse shared terms ({asset.items.length})</summary><div class="term-list">{#each asset.items as term (term.id)}<div class="term"><strong>{term.source_term} → {term.target_term}</strong>{#if term.sense}<span>{term.sense}</span>{/if}{#if term.notes}<small>{term.notes}</small>{/if}</div>{/each}</div></details>
  {/if}
  {#if comparison}
    <section class="comparison" aria-label="Review glossary changes">
      <h3>{comparison.mode === "copy" ? "Choose terms to copy" : "Review terminology changes"}</h3>
      <p class="hint">Review each changed term. Keeping a local choice records an exception. A shared refinement may apply to a different sense; matching wording alone does not establish that it belongs in this book.</p>
      <div class="actions"><button type="button" disabled={busy} on:click={selectSuggested}>Select non-conflicting updates</button><label class="check"><input type="checkbox" bind:checked={showUnchanged} /> Show unchanged terms</label></div>
      <label>Find a term<input type="search" bind:value={compareQuery} /></label>
      <div class="change-list">
        {#each visibleRows as row (row.id)}
          <article class="change">
            <div class="change-heading"><strong>{row.incoming?.source_term || row.local?.source_term || row.base?.source_term}</strong><span class:conflict={row.status === "conflict"}>{({ add: "New term", update: "Shared update", unchanged: "Already aligned", local: "Local exception", conflict: "Needs a decision", retired: "Retired from shared glossary" })[row.status] || row.status}</span></div>
            {#if row.incoming?.sense || row.local?.sense}<p class="hint">Sense: {row.incoming?.sense || row.local?.sense}</p>{/if}
            <div class="renderings">
              {#each [{ label: "This book", term: row.local }, { label: `Shared revision ${comparison.revision}`, term: row.incoming }] as side}
                <div><small>{side.label}</small><p>{side.term?.target_term || (side.label === "This book" ? "No term yet" : "Retired")}</p>
                  {#if side.term?.notes}<p class="hint">{side.term.notes}</p>{/if}
                  {#if side.term}<details><summary>Usage details</summary><p class="hint">Source term: {side.term.source_term}</p><p class="hint">Sense: {side.term.sense || "Not specified"}</p><p class="hint">{side.term.match_policy === "exact" ? "Fixed spelling" : "Inflect grammatically"}</p><p class="hint">Source forms: {(side.term.source_forms || []).join(", ") || side.term.source_term}</p><p class="hint">Accepted target variants: {(side.term.accepted_target_forms || []).join(", ") || "None specified"}</p></details>{/if}
                </div>
              {/each}
            </div>
            {#if row.base}<details><summary>Previously adopted choice</summary><p>{row.base.source_term} → {row.base.target_term}</p>{#if row.base.notes}<p class="hint">{row.base.notes}</p>{/if}</details>{/if}
            {#if row.status !== "unchanged" || comparison.mode === "copy"}
              <label>Decision for {row.incoming?.source_term || row.local?.source_term || row.base?.source_term}
                <select value={choices[row.id] || ""} disabled={busy} on:change={(event) => setChoice(row.id, event.currentTarget.value)}>
                  <option value="">Choose…</option>
                  {#if row.can_take_incoming}<option value="incoming">Use shared term</option>{/if}
                  <option value="local">{row.local ? "Keep this book’s choice" : "Skip this term"}</option>
                </select>
              </label>
              {#if !row.can_take_incoming && row.local?.authority === "human_locked"}<p class="hint">This term is human-locked. Review and edit it in “This book” before adopting a different choice.</p><button type="button" disabled={busy} on:click={() => onReviewLocked(row.local.concept_id)}>Review this locked term</button>{/if}
            {/if}
          </article>
        {/each}
        {#if !visibleRows.length}<p class="hint">No changed terms match this view.</p>{/if}
      </div>
      <div class="actions"><button type="button" disabled={busy} on:click={() => comparison = null}>Cancel comparison</button><button type="button" class="primary" disabled={busy || operationActive || (comparison.mode === "copy" ? !incomingCount : unresolved > 0)} on:click={applyComparison}>{comparison.mode === "copy" ? `Copy ${incomingCount} ${incomingCount === 1 ? "term" : "terms"}` : "Adopt reviewed revision"}</button></div>
      {#if comparison.mode !== "copy" && unresolved}<p class="hint">{unresolved} changed {unresolved === 1 ? "term still needs" : "terms still need"} a decision, including any hidden by the search.</p>{/if}
    </section>
  {/if}
  <details class="contribution-tools">
    <summary>Create a glossary or contribute this book’s refinements</summary>
    <p class="hint">Choose reusable terms. Book-specific quotations and occurrence anchors stay with their source book. Creating or publishing shared terminology does not create human locks or change another book.</p>
    <label>Find this book’s terms<input type="search" bind:value={termQuery} /></label>
    <div class="actions"><span>{selectedTerms.length} selected</span><button type="button" disabled={busy || !visibleTerms.length} on:click={() => selectedTerms = [...new Set([...selectedTerms, ...visibleTerms.map((term) => term.concept_id)])]}>Select visible terms</button><button type="button" disabled={busy || !selectedTerms.length} on:click={() => selectedTerms = []}>Clear selection</button></div>
    <div class="term-list">{#each visibleTerms as term (term.concept_id)}<label class="term selectable"><input type="checkbox" checked={selectedTerms.includes(term.concept_id)} disabled={busy} on:change={() => toggleTerm(term.concept_id)} /><span><strong>{term.source_term} → {term.target_term}</strong>{#if term.sense}<small>{term.sense}</small>{/if}<small>{term.authority === "human_locked" ? "Human-locked in this book" : "Working suggestion"}</small></span></label>{/each}{#if !visibleTerms.length}<p class="hint">No terms yet. Add terms in “This book” or let translation build the working glossary.</p>{/if}</div>
    <form on:submit|preventDefault={createAsset}><label>New shared glossary name<input bind:value={name} required maxlength="255" placeholder="For example: Collected essays — German to English" /></label><button type="submit" disabled={busy || !name.trim() || !selectedTerms.length}>Create from selected terms</button></form>
    {#if asset}<form on:submit|preventDefault={contribute}><label>Why should these changes be shared?<textarea bind:value={reason} required maxlength="4000" rows="2" placeholder="Explain the refinement and where it applies."></textarea></label><button type="submit" disabled={busy || !reason.trim() || !selectedTerms.length}>Prepare contribution to {asset.name}</button></form>{/if}
  </details>
  {#if asset?.proposals?.length}
    <section class="proposals" aria-label="Contributions awaiting review"><h3>Contributions awaiting review</h3><p class="hint">Publishing creates a new shared revision. Other books receive an available update, not an automatic edit.</p>
      {#each asset.proposals as proposal (proposal.id)}<article class="proposal"><p>{proposal.reason}</p><p class="hint">Prepared against revision {proposal.base_revision}</p><div class="term-list">{#each proposal.changes || [] as change (change.id)}<div class="term"><strong>{change.after?.source_term || change.before?.source_term}</strong><span>{change.before?.target_term || "New term"} → {change.after?.target_term || "Retire term"}</span><details><summary>Review all term details</summary><div class="renderings">{#each [{ label: "Before", term: change.before }, { label: "Proposed", term: change.after }] as side}<div><strong>{side.label}</strong>{#if side.term}<p>{side.term.source_term} → {side.term.target_term}</p><p class="hint">Sense: {side.term.sense || "Not specified"}</p><p class="hint">Notes: {side.term.notes || "None"}</p><p class="hint">{side.term.match_policy === "exact" ? "Fixed spelling" : "Inflect grammatically"}</p><p class="hint">Source forms: {(side.term.source_forms || []).join(", ") || side.term.source_term}</p><p class="hint">Accepted target variants: {(side.term.accepted_target_forms || []).join(", ") || "None specified"}</p>{:else}<p class="hint">No term</p>{/if}</div>{/each}</div></details></div>{/each}</div><div class="actions"><button type="button" disabled={busy} on:click={() => review(proposal, "reject")}>Reject contribution</button><button type="button" class="primary" disabled={busy || proposal.base_revision !== asset.revision} on:click={() => review(proposal, "accept")}>Publish shared revision</button></div>{#if proposal.base_revision !== asset.revision}<p class="hint">The shared glossary changed. Reconcile this book with the latest revision, then prepare a fresh contribution.</p>{/if}</article>{/each}
    </section>
  {/if}
  <div class="check-translation"><p>Adopting terminology does not rewrite completed translation. Check existing passages, then edit or retranslate the affected blocks.</p><button type="button" disabled={busy} on:click={onCheckTranslation}>Check existing translation</button></div>
</section>

<style>
  .shared-panel { display:grid; gap:1rem; color:var(--color-ink); }
  p, h3 { margin:0; }
  h3 { font-size:1rem; }
  .intro, .check-translation p { font-size:.88rem; line-height:1.55; }
  .hint, small, summary { color:var(--color-ink-muted); font-size:.8rem; line-height:1.5; }
  .actions, .binding, .change-heading { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.65rem; }
  .asset-picker { display:flex; align-items:end; gap:.7rem; }
  .asset-picker > label { flex:1; min-width:0; }
  label, form { display:grid; gap:.45rem; font-size:.83rem; }
  form { gap:.7rem; padding-top:1rem; }
  button, input, select, textarea { font:inherit; color:var(--color-ink); border:1px solid var(--color-rule); background:var(--color-surface); padding:.55rem .7rem; border-radius:.2rem; }
  input, select, textarea { width:100%; box-sizing:border-box; min-width:0; }
  button { cursor:pointer; font-size:.82rem; }
  button:disabled { opacity:.55; cursor:not-allowed; }
  button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible { outline:2px solid var(--color-clay); outline-offset:2px; }
  .primary { background:var(--color-clay); color:var(--color-surface); }
  .message, .binding, .check-translation { padding:.85rem; border:1px solid var(--color-rule); background:var(--color-surface-muted); }
  .message p + .actions { margin-top:.7rem; }
  .error, .conflict { color:var(--color-danger); }
  .binding p { font-size:.8rem; margin-top:.3rem; }
  details > summary { cursor:pointer; font-weight:600; padding:.4rem 0; }
  details > p { margin:.5rem 0; }
  .comparison, .proposals { display:grid; gap:.8rem; padding-top:.8rem; border-top:1px solid var(--color-rule); }
  .change-list, .term-list { max-height:26rem; overflow:auto; border:1px solid var(--color-rule); }
  .change, .proposal { display:grid; gap:.7rem; padding:1rem; border-bottom:1px solid var(--color-rule); }
  .change-heading > span { font-size:.75rem; }
  .renderings { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
  .renderings p, .term { overflow-wrap:anywhere; }
  .renderings p { margin-top:.3rem; }
  .term { display:grid; gap:.3rem; padding:.7rem; border-bottom:1px solid var(--color-rule); font-size:.85rem; }
  .term:last-child, .change:last-child { border-bottom:0; }
  .selectable, .check { display:flex; align-items:start; gap:.65rem; }
  .selectable input, .check input { width:auto; margin-top:.25rem; }
  .selectable span { display:grid; gap:.2rem; }
  .contribution-tools { border-top:1px solid var(--color-rule); padding-top:.7rem; }
  .contribution-tools > label, .contribution-tools > .actions { margin:.7rem 0; }
  .check-translation { display:grid; gap:.7rem; }
  @media (max-width:650px) { .renderings { grid-template-columns:1fr; } .asset-picker { align-items:stretch; flex-direction:column; } .actions > button { flex-grow:1; } }
</style>
