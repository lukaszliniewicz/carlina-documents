<script>
  import { Dialog } from "@carlina/ui";
  import { ANNOTATION_PROFILES, normalizeAnnotationProfile } from "../lib/annotation-profile.js";

  export let open = false;
  export let selectionCount = 0;
  export let selectedBlockIds = [];
  export let accessRole = "owner";
  export let dossiers = [];
  export let defaultAnnotationProfile = "none";
  export let busy = false;
  export let error = "";
  export let onClose = () => {};
  export let onSubmit = () => {};

  let customPrompt = "";
  let annotationProfile = "none";
  let researchChoice = "reuse_dossier";
  let dossierRevisionId = "";
  let rationale = "";
  let previousOpen = false;

  function dossierCovers(dossier) {
    if (dossier?.scope?.kind === "document") return true;
    const covered = new Set(
      Array.isArray(dossier?.scope?.block_ids)
        ? dossier.scope.block_ids.map(String)
        : []
    );
    return selectedBlockIds.every((blockId) => covered.has(String(blockId)));
  }

  $: eligibleDossiers = dossiers.filter(dossierCovers);

  $: if (open && !previousOpen) {
    annotationProfile = normalizeAnnotationProfile(defaultAnnotationProfile);
    dossierRevisionId = eligibleDossiers[0]?.revision_id || "";
    researchChoice = dossierRevisionId ? "reuse_dossier" : "await_external";
    customPrompt = "";
    rationale = "";
  }
  $: previousOpen = open;
  $: owner = accessRole === "owner";
  $: waitsForExternal = researchChoice === "await_external";
  $: dossierRequired = researchChoice === "reuse_dossier";
  $: if (
    open
    && dossierRequired
    && !eligibleDossiers.some((dossier) => dossier.revision_id === dossierRevisionId)
  ) {
    dossierRevisionId = eligibleDossiers[0]?.revision_id || "";
  }
  $: valid = selectionCount > 0
    && (!dossierRequired || Boolean(dossierRevisionId));

  function submit(event) {
    event.preventDefault();
    if (busy || !valid) return;
    onSubmit({
      custom_prompt: customPrompt.trim() || null,
      annotation_profile: annotationProfile,
      research_choice: researchChoice,
      dossier_revision_id: dossierRequired ? dossierRevisionId : null,
      rationale: rationale.trim() || null,
      execute_now: owner && !waitsForExternal
    });
  }
</script>

<Dialog
  {open}
  title={owner ? "Retranslate selected blocks" : "Request retranslation"}
  description={`${selectionCount} ordered block${selectionCount === 1 ? "" : "s"} will be frozen against the active source plan. Unselected targets remain protected.`}
  onclose={() => !busy && onClose()}
  size="lg"
>
  <form class="retranslation-form" on:submit={submit}>
    {#if error}<div class="alert error" role="alert">{error}</div>{/if}

    {#if !owner}
      <p class="notice">A Reviewer can record the exact scope and rationale, but cannot spend model or research capacity. The owner will inspect and execute the request.</p>
    {/if}

    {#if owner}
      <p class="notice">Execution uses the project&rsquo;s current translator model and reasoning settings. This request freezes only the selected blocks, its instruction, annotation profile, and research choice.</p>
    {/if}

    <fieldset class="annotation-profile">
      <legend>Notes for this operation</legend>
      {#each ANNOTATION_PROFILES as profile}
        <label class="profile-option">
          <input type="radio" name="retranslation-annotation-profile" value={profile.value} bind:group={annotationProfile} disabled={busy} />
          <span><strong>{profile.label}</strong><small>{profile.description}</small></span>
        </label>
      {/each}
    </fieldset>

    <label>
      Research context
      <select bind:value={researchChoice} disabled={busy}>
        <option value="reuse_dossier" disabled={!eligibleDossiers.length}>Reuse a saved dossier covering every selected block</option>
        <option value="fresh_document">Run fresh whole-document research</option>
        <option value="await_external">Wait for a new externally prepared dossier</option>
      </select>
      <small>Selection-scoped research is submitted as a dossier, so the evidence and conclusions remain reviewable instead of being hidden inside one model call.</small>
    </label>

    {#if dossierRequired}
      <label>
        Dossier revision
        <select bind:value={dossierRevisionId} disabled={busy || !eligibleDossiers.length} required>
          {#each eligibleDossiers as dossier}
            <option value={dossier.revision_id}>{`Dossier ${(dossier.dossier_id || dossier.id).slice(0, 8)}`} · revision {dossier.version} · {dossier.conclusion_provenance === "external_unverified" ? "external, unverified" : "managed"}</option>
          {/each}
        </select>
        {#if !eligibleDossiers.length}<small>No saved dossier covers the full selected scope. Choose fresh whole-document research or wait for a new external dossier.</small>{/if}
      </label>
    {/if}

    <label>
      Operation-only instruction <small>Optional · 20,000 characters</small>
      <textarea bind:value={customPrompt} rows="5" maxlength="20000" disabled={busy} placeholder="Clarify only what should change in these selected blocks."></textarea>
    </label>

    <label>
      Rationale for the review record <small>Optional · 4,000 characters</small>
      <textarea bind:value={rationale} rows="3" maxlength="4000" disabled={busy} placeholder="What is wrong or what should the next pass investigate?"></textarea>
    </label>

    <div class="form-actions">
      <button type="button" class="btn secondary" disabled={busy} on:click={onClose}>Cancel</button>
      <button type="submit" class="btn primary" disabled={busy || !valid}>{busy ? "Recording…" : owner ? waitsForExternal ? "Record and wait for dossier" : "Record and start" : "Send request to owner"}</button>
    </div>
  </form>
</Dialog>

<style>
  .retranslation-form { display:grid; gap:.85rem; }
  label { display:grid; gap:.34rem; color:var(--color-ink-soft); font-size:.82rem; font-weight:650; }
  label small { color:var(--color-ink-muted); font-weight:400; line-height:1.4; }
  select, textarea { width:100%; box-sizing:border-box; border:1px solid var(--color-rule-strong); padding:.56rem .62rem; background:var(--color-surface); color:var(--color-ink); font:inherit; }
  textarea { resize:vertical; line-height:1.48; }
  .notice { margin:0; padding:.65rem .72rem; border:1px solid color-mix(in srgb,var(--color-active) 30%,var(--color-rule)); background:var(--color-active-muted); color:var(--color-ink-soft); font-size:.8rem; line-height:1.45; }
  .alert { padding:.65rem .72rem; border:1px solid transparent; font-size:.82rem; }
  .alert.error { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); }
  .annotation-profile { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.5rem; margin:0; padding:.7rem; border:1px solid var(--color-rule); }
  .annotation-profile legend { padding:0 .28rem; color:var(--color-ink-soft); font-size:.82rem; font-weight:650; }
  .profile-option { display:grid; grid-template-columns:auto minmax(0,1fr); align-items:start; gap:.48rem; padding:.5rem; border:1px solid var(--color-rule); background:var(--color-surface); cursor:pointer; }
  .profile-option:focus-within { outline:2px solid color-mix(in srgb,var(--color-active) 28%,transparent); }
  .profile-option input { width:1rem; height:1rem; margin:.12rem 0 0; accent-color:var(--color-active); }
  .profile-option span { display:grid; gap:.14rem; }
  .profile-option strong { color:var(--color-ink); font-size:.8rem; }
  .profile-option small { font-size:.72rem; }
  .form-actions { display:flex; justify-content:flex-end; gap:.45rem; }
  .btn { min-height:2.2rem; border:1px solid var(--color-rule); padding:.48rem .72rem; background:var(--color-surface); color:var(--color-ink); cursor:pointer; font:inherit; font-size:.77rem; font-weight:650; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  @media (max-width:760px) { .annotation-profile { grid-template-columns:1fr; } }
</style>
