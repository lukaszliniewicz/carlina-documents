<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let requests = [];
  export let dossiers = [];
  export let accessRole = "owner";
  export let busyRequestId = "";
  export let error = "";
  export let onClose = () => {};
  export let onAttach = () => {};
  export let onExecute = () => {};

  let dossierSelections = {};

  $: owner = accessRole === "owner";
  $: orderedRequests = [...requests].sort((left, right) =>
    String(right?.created_at || "").localeCompare(String(left?.created_at || ""))
  );

  function scopeLabel(request) {
    const count = Array.isArray(request?.resolved_block_ids) ? request.resolved_block_ids.length : 0;
    return `${count} block${count === 1 ? "" : "s"}`;
  }

  function researchLabel(choice) {
    if (choice === "reuse_dossier") return "Saved research dossier";
    if (choice === "fresh_document") return "Fresh whole-document research";
    if (choice === "await_external") return "Awaiting external research";
    return "Research choice unavailable";
  }

  function stateLabel(state) {
    return {
      requested: "Ready for owner",
      started: "Started",
      completed: "Completed",
      rejected: "Rejected",
      cancelled: "Cancelled"
    }[state] || "Recorded";
  }

  function dossierCovers(request, dossier) {
    if (dossier?.scope?.kind === "document") return true;
    const covered = new Set(Array.isArray(dossier?.scope?.block_ids) ? dossier.scope.block_ids.map(String) : []);
    return (request?.resolved_block_ids || []).every((blockId) => covered.has(String(blockId)));
  }

  function eligibleDossiers(request) {
    return dossiers.filter((dossier) => dossierCovers(request, dossier));
  }

  function selectedDossier(request) {
    const eligible = eligibleDossiers(request);
    const selected = dossierSelections[request.id];
    return eligible.some((dossier) => dossier.revision_id === selected)
      ? selected
      : eligible[0]?.revision_id || "";
  }

  function changeDossier(requestId, revisionId) {
    dossierSelections = { ...dossierSelections, [requestId]: revisionId };
  }
</script>

<Dialog
  {open}
  title="Retranslation requests"
  description={owner
    ? "Inspect recorded scopes and evidence choices before starting model work. Reviewer requests never execute by themselves."
    : "Your requests are durable review records. Only the project owner can spend model or research capacity."}
  onclose={() => !busyRequestId && onClose()}
  size="xl"
>
  <div class="request-shell">
    {#if error}<div class="alert error" role="alert">{error}</div>{/if}

    {#if orderedRequests.length}
      <ol>
        {#each orderedRequests as request (request.id)}
          <li class:terminal={["completed", "rejected", "cancelled"].includes(request.state)}>
            <header>
              <div>
                <p class="eyebrow">{scopeLabel(request)} · {request.annotation_profile || "none"} notes</p>
                <h3>{researchLabel(request.research_choice)}</h3>
              </div>
              <span class={`state ${request.state}`}>{stateLabel(request.state)}</span>
            </header>

            {#if request.rationale}<p><strong>Rationale:</strong> {request.rationale}</p>{/if}
            {#if request.custom_prompt}<p><strong>Operation instruction:</strong> {request.custom_prompt}</p>{/if}
            <p class="meta">Recorded {request.created_at ? new Date(request.created_at).toLocaleString() : "recently"} · revision {request.version}</p>

            {#if owner && request.state === "requested" && request.research_choice === "await_external"}
              {@const eligible = eligibleDossiers(request)}
              <div class="attach-row">
                <label>
                  Research dossier covering this scope
                  <select
                    value={selectedDossier(request)}
                    disabled={busyRequestId === request.id || !eligible.length}
                    on:change={(event) => changeDossier(request.id, event.currentTarget.value)}
                  >
                    {#each eligible as dossier (dossier.revision_id)}
                      <option value={dossier.revision_id}>Dossier {(dossier.dossier_id || dossier.id).slice(0, 8)} · revision {dossier.version}</option>
                    {/each}
                  </select>
                </label>
                <button
                  type="button"
                  class="btn secondary"
                  disabled={busyRequestId === request.id || !selectedDossier(request)}
                  on:click={() => onAttach(request, selectedDossier(request))}
                >
                  {busyRequestId === request.id ? "Attaching…" : "Attach evidence"}
                </button>
              </div>
              {#if !eligible.length}<p class="hint">Record a whole-document dossier or one covering every requested block, then return here.</p>{/if}
            {:else if owner && request.state === "requested"}
              <div class="actions">
                <button
                  type="button"
                  class="btn primary"
                  disabled={Boolean(busyRequestId)}
                  on:click={() => onExecute(request)}
                >
                  {busyRequestId === request.id ? "Starting…" : "Start exact request"}
                </button>
              </div>
            {:else if owner && request.state === "started"}
              <div class="actions">
                <button
                  type="button"
                  class="btn secondary"
                  disabled={Boolean(busyRequestId)}
                  on:click={() => onExecute(request)}
                >
                  {busyRequestId === request.id ? "Checking…" : "Check or retry operation"}
                </button>
              </div>
            {/if}
          </li>
        {/each}
      </ol>
    {:else}
      <p class="empty">No retranslation requests have been recorded for this project.</p>
    {/if}

    <div class="dialog-actions">
      <button type="button" class="btn" disabled={Boolean(busyRequestId)} on:click={onClose}>Close</button>
    </div>
  </div>
</Dialog>

<style>
  .request-shell { display:grid; gap:.85rem; }
  .alert { padding:.65rem .75rem; border:1px solid transparent; font-size:.82rem; }
  .alert.error { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); }
  ol { display:grid; gap:.65rem; margin:0; padding:0; list-style:none; }
  li { display:grid; gap:.58rem; padding:.82rem; border:1px solid var(--color-rule); background:var(--color-surface); }
  li.terminal { opacity:.72; }
  header { display:flex; justify-content:space-between; align-items:start; gap:1rem; }
  .eyebrow { margin:0 0 .15rem; color:var(--color-clay); font-size:.61rem; font-weight:800; letter-spacing:.1em; text-transform:uppercase; }
  h3 { margin:0; color:var(--color-ink); font-family:var(--font-editorial,Georgia,serif); font-size:1rem; font-weight:550; }
  p { margin:0; color:var(--color-ink-muted); font-size:.76rem; line-height:1.48; white-space:pre-wrap; overflow-wrap:anywhere; }
  p strong { color:var(--color-ink-soft); }
  .meta, .hint { font-size:.68rem; }
  .state { flex:0 0 auto; padding:.25rem .38rem; border:1px solid var(--color-rule); color:var(--color-ink-muted); font-size:.6rem; font-weight:800; letter-spacing:.07em; text-transform:uppercase; }
  .state.requested { border-color:color-mix(in srgb,var(--color-active) 45%,var(--color-rule)); color:var(--color-active); }
  .state.started { border-color:color-mix(in srgb,var(--color-clay) 45%,var(--color-rule)); color:var(--color-clay); }
  .state.completed { color:var(--color-success,#34745a); }
  .attach-row { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:end; gap:.55rem; padding-top:.2rem; }
  label { display:grid; gap:.3rem; color:var(--color-ink-soft); font-size:.72rem; font-weight:650; }
  select { width:100%; min-height:2.25rem; box-sizing:border-box; border:1px solid var(--color-rule-strong); padding:.48rem .55rem; background:var(--color-surface); color:var(--color-ink); font:inherit; }
  .actions, .dialog-actions { display:flex; justify-content:flex-end; gap:.45rem; }
  .btn { min-height:2.2rem; border:1px solid var(--color-rule); padding:.48rem .72rem; background:var(--color-surface); color:var(--color-ink); cursor:pointer; font:inherit; font-size:.75rem; font-weight:650; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .btn.secondary { border-color:var(--color-umber); color:var(--color-umber); }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .empty { padding:1.2rem; border:1px dashed var(--color-rule); text-align:center; }
  @media (max-width:700px) { .attach-row { grid-template-columns:1fr; } .attach-row .btn { width:100%; } }
</style>
