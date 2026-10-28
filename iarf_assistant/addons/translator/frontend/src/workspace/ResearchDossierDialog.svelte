<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let dossiers = [];
  export let selectedBlockIds = [];
  export let accessRole = "owner";
  export let busy = false;
  export let error = "";
  export let onClose = () => {};
  export let onInspect = async () => null;
  export let onSubmit = () => {};

  const EMPTY_PAYLOAD = {
    document_brief: "",
    style_guidance: [],
    entities: [],
    terminology: [],
    references: [],
    allusions: [],
    unresolved_questions: [],
    annotation_proposals: []
  };

  let scopeKind = "document";
  let method = "external research submission";
  let models = "";
  let agents = "";
  let provenanceNotes = "";
  let payloadText = JSON.stringify(EMPTY_PAYLOAD, null, 2);
  let parseError = "";
  let previousOpen = false;
  let submissionId = "";
  let expandedRevisionId = "";
  let detailsByRevision = {};
  let inspectingRevisionId = "";
  let inspectError = "";

  function newSubmissionId() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    return `external-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  $: if (open && !previousOpen) {
    scopeKind = selectedBlockIds.length ? "blocks" : "document";
    method = "external research submission";
    models = "";
    agents = "";
    provenanceNotes = "";
    payloadText = JSON.stringify(EMPTY_PAYLOAD, null, 2);
    parseError = "";
    submissionId = newSubmissionId();
    expandedRevisionId = "";
    inspectError = "";
  }
  $: previousOpen = open;

  function splitNames(value) {
    return value.split(",").map((item) => item.trim()).filter(Boolean).slice(0, 64);
  }

  function dossierKey(dossier) {
    return String(dossier?.revision_id || dossier?.id || dossier?.dossier_id || "");
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  async function toggleDetails(dossier) {
    const key = dossierKey(dossier);
    if (!key) return;
    if (expandedRevisionId === key) {
      expandedRevisionId = "";
      inspectError = "";
      return;
    }
    expandedRevisionId = key;
    inspectError = "";
    if (detailsByRevision[key]) return;
    inspectingRevisionId = key;
    try {
      const detail = await onInspect(dossier);
      if (!detail || typeof detail !== "object" || !detail.payload) {
        throw new Error("The dossier revision returned no reviewable conclusions.");
      }
      detailsByRevision = { ...detailsByRevision, [key]: detail };
    } catch (caught) {
      inspectError = caught?.message || "Unable to load this dossier revision.";
    } finally {
      inspectingRevisionId = "";
    }
  }

  function submit(event) {
    event.preventDefault();
    if (busy) return;
    let payload;
    try {
      payload = JSON.parse(payloadText);
    } catch {
      parseError = "The research packet is not valid JSON.";
      return;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      parseError = "The research packet must be one JSON object.";
      return;
    }
    parseError = "";
    onSubmit({
      client_submission_id: submissionId,
      dossier_id: null,
      expected_revision: 0,
      scope: {
        kind: scopeKind,
        block_ids: scopeKind === "blocks" ? [...selectedBlockIds] : []
      },
      provenance: {
        method: method.trim(),
        models: splitNames(models),
        agents: splitNames(agents),
        notes: provenanceNotes.trim()
      },
      payload
    });
  }
</script>

<Dialog
  {open}
  title="Research dossiers"
  description="Persist conclusions prepared outside Carlina. Every submission remains explicitly external and unverified; terminology and note proposals enter the normal review queues."
  onclose={() => !busy && onClose()}
  size="xl"
>
  <div class="dossier-shell">
    {#if error || parseError}<div class="alert error" role="alert">{parseError || error}</div>{/if}

    <section class="saved" aria-labelledby="saved-dossiers-title">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Durable evidence</p>
          <h3 id="saved-dossiers-title">Saved revisions</h3>
        </div>
        <span>{dossiers.length}</span>
      </div>
      {#if dossiers.length}
        <ol>
          {#each dossiers as dossier (dossier.revision_id)}
            <li>
              <div class="revision-heading"><strong>Dossier {(dossier.dossier_id || dossier.id).slice(0, 8)}</strong><span>Revision {dossier.version}</span></div>
              <p>{dossier.scope?.kind === "blocks" ? `${dossier.scope.block_ids?.length || 0} selected blocks` : "Whole document"} · external, unverified{#if dossier.submitted_at} · {new Date(dossier.submitted_at).toLocaleString()}{/if}</p>
              <button
                type="button"
                class="inspect-button"
                aria-expanded={expandedRevisionId === dossierKey(dossier)}
                disabled={inspectingRevisionId === dossierKey(dossier)}
                on:click={() => toggleDetails(dossier)}
              >
                {inspectingRevisionId === dossierKey(dossier)
                  ? "Loading conclusions…"
                  : expandedRevisionId === dossierKey(dossier)
                    ? "Hide conclusions"
                    : "View conclusions"}
              </button>
              {#if expandedRevisionId === dossierKey(dossier)}
                {#if inspectError}
                  <p class="detail-error" role="alert">{inspectError}</p>
                {:else if detailsByRevision[dossierKey(dossier)]}
                  {@const detail = detailsByRevision[dossierKey(dossier)]}
                  {@const payload = detail.payload}
                  {@const provenance = detail.provenance || dossier.provenance || {}}
                  <div class="dossier-detail" aria-label="Research conclusions">
                    {#if provenance.method || list(provenance.models).length || list(provenance.agents).length || provenance.notes}
                      <section>
                        <h4>Provenance</h4>
                        {#if provenance.method}<p><strong>Method:</strong> {provenance.method}</p>{/if}
                        {#if list(provenance.models).length}<p><strong>Models:</strong> {list(provenance.models).join(", ")}</p>{/if}
                        {#if list(provenance.agents).length}<p><strong>Agents:</strong> {list(provenance.agents).join(", ")}</p>{/if}
                        {#if provenance.notes}<p>{provenance.notes}</p>{/if}
                      </section>
                    {/if}
                    {#if payload.document_brief}
                      <section><h4>Document brief</h4><p>{payload.document_brief}</p></section>
                    {/if}
                    {#if list(payload.style_guidance).length}
                      <section><h4>Style guidance</h4><ul>{#each list(payload.style_guidance) as guidance}<li>{guidance}</li>{/each}</ul></section>
                    {/if}
                    {#if list(payload.entities).length}
                      <section><h4>Entities</h4><dl>{#each list(payload.entities) as entity}<div><dt>{entity.name} <small>{entity.kind}</small></dt><dd>{entity.description}</dd></div>{/each}</dl></section>
                    {/if}
                    {#if list(payload.terminology).length}
                      <section><h4>Terminology</h4><dl>{#each list(payload.terminology) as term}<div><dt>{term.source_term}{#if term.target_term} <span aria-hidden="true">→</span> {term.target_term}{/if}</dt><dd>{term.notes || "No usage note supplied."}{#if typeof term.confidence === "number"} <small>Confidence {Math.round(term.confidence * 100)}%</small>{/if}</dd></div>{/each}</dl></section>
                    {/if}
                    {#if list(payload.allusions).length}
                      <section><h4>Allusions</h4><dl>{#each list(payload.allusions) as allusion}<div><dt>{allusion.label}</dt><dd>{allusion.explanation}</dd></div>{/each}</dl></section>
                    {/if}
                    {#if list(payload.unresolved_questions).length}
                      <section><h4>Unresolved questions</h4><ul>{#each list(payload.unresolved_questions) as question}<li>{question.question}</li>{/each}</ul></section>
                    {/if}
                    {#if list(payload.annotation_proposals).length}
                      <section><h4>Annotation proposals</h4><ul>{#each list(payload.annotation_proposals) as note}<li><strong>{note.anchor_text || "Whole block"}</strong> — {note.body}</li>{/each}</ul></section>
                    {/if}
                    {#if list(payload.references).length}
                      <section><h4>References</h4><dl>{#each list(payload.references) as reference}<div><dt>{#if reference.url}<a href={reference.url} target="_blank" rel="noopener noreferrer">{reference.title}</a>{:else}{reference.title}{/if}</dt><dd>{reference.authors}{#if reference.locator} · {reference.locator}{/if}{#if reference.note}<br />{reference.note}{/if}</dd></div>{/each}</dl></section>
                    {/if}
                    {#if !payload.document_brief && !list(payload.style_guidance).length && !list(payload.entities).length && !list(payload.terminology).length && !list(payload.allusions).length && !list(payload.unresolved_questions).length && !list(payload.annotation_proposals).length && !list(payload.references).length}
                      <p class="empty">This revision records an empty conclusion packet.</p>
                    {/if}
                  </div>
                {/if}
              {/if}
            </li>
          {/each}
        </ol>
      {:else}
        <p class="empty">No external research has been recorded for this source plan.</p>
      {/if}
    </section>

    {#if accessRole === "owner"}
    <form on:submit={submit}>
      <div class="section-heading">
        <div>
          <p class="eyebrow">New immutable revision</p>
          <h3>Submit conclusions</h3>
        </div>
      </div>

      <div class="form-grid">
        <label>
          Scope
          <select bind:value={scopeKind} disabled={busy || !selectedBlockIds.length}>
            <option value="document">Whole document</option>
            <option value="blocks" disabled={!selectedBlockIds.length}>Current selection ({selectedBlockIds.length})</option>
          </select>
          {#if !selectedBlockIds.length}<small>Select blocks in the workspace first to submit a bounded dossier.</small>{/if}
        </label>
        <label>
          Research method
          <input bind:value={method} maxlength="512" required disabled={busy} />
        </label>
        <label>
          Models <small>Comma separated</small>
          <input bind:value={models} maxlength="4096" placeholder="e.g. GPT-5.6, DeepSeek V4" disabled={busy} />
        </label>
        <label>
          Agents <small>Comma separated</small>
          <input bind:value={agents} maxlength="4096" placeholder="e.g. terminology, allusions" disabled={busy} />
        </label>
      </div>

      <label>
        Provenance note <small>Optional</small>
        <textarea bind:value={provenanceNotes} rows="2" maxlength="8000" disabled={busy} placeholder="Sources, limits, or review caveats about how this packet was produced."></textarea>
      </label>

      <label>
        Structured research packet
        <textarea class="json-editor" bind:value={payloadText} rows="20" spellcheck="false" disabled={busy}></textarea>
        <small>Use the supplied keys for document brief, style guidance, entities, terminology, references, allusions, unresolved questions, and annotation proposals. Block and reference IDs are checked server-side.</small>
      </label>

      <div class="notice">
        <strong>Authority boundary</strong>
        <span>This records evidence; it does not certify the conclusions. Proposed glossary terms stay non-binding until the owner approves them, and proposed reader notes remain visibly reviewable.</span>
      </div>

      <div class="actions">
        <button type="button" class="btn" disabled={busy} on:click={onClose}>Close</button>
        <button type="submit" class="btn primary" disabled={busy || !method.trim()}>{busy ? "Recording…" : "Record dossier"}</button>
      </div>
    </form>
    {:else}
      <section class="reader-notice">
        <strong>Read-only research record</strong>
        <p>You can inspect the conclusions and provenance used during review. Only the project owner can append a dossier revision.</p>
      </section>
    {/if}
  </div>
</Dialog>

<style>
  .dossier-shell { display:grid; grid-template-columns:minmax(15rem,.7fr) minmax(0,1.5fr); gap:1rem; align-items:start; }
  .saved, form { display:grid; gap:.75rem; padding:.9rem; border:1px solid var(--color-rule); background:var(--color-surface); }
  .section-heading { display:flex; align-items:start; justify-content:space-between; gap:1rem; }
  .section-heading > span { color:var(--color-active); font-size:.75rem; font-weight:800; }
  .eyebrow { margin:0; color:var(--color-ink-muted); font-size:.61rem; font-weight:800; letter-spacing:.13em; text-transform:uppercase; }
  h3 { margin:.16rem 0 0; color:var(--color-ink); font-family:var(--font-editorial,Georgia,serif); font-size:1.04rem; font-weight:550; }
  ol { display:grid; gap:.45rem; margin:0; padding:0; list-style:none; }
  li { padding:.62rem; border:1px solid var(--color-rule); background:var(--color-surface-muted); }
  .revision-heading { display:flex; justify-content:space-between; gap:.6rem; }
  li strong { color:var(--color-ink-soft); font-size:.78rem; }
  li span { color:var(--color-ink-muted); font-size:.68rem; }
  li p, .empty { margin:.25rem 0 0; color:var(--color-ink-muted); font-size:.7rem; line-height:1.42; }
  .inspect-button { margin-top:.5rem; padding:0; border:0; border-bottom:1px solid currentColor; background:transparent; color:var(--color-clay); cursor:pointer; font:inherit; font-size:.7rem; font-weight:700; }
  .inspect-button:disabled { opacity:.55; cursor:wait; }
  .dossier-detail { display:grid; gap:.65rem; margin-top:.62rem; padding:.7rem; border-top:1px solid var(--color-rule); background:var(--color-surface); }
  .dossier-detail section { display:grid; gap:.25rem; }
  .dossier-detail h4 { margin:0; color:var(--color-ink-soft); font-size:.72rem; letter-spacing:.06em; text-transform:uppercase; }
  .dossier-detail p, .dossier-detail ul, .dossier-detail dl { margin:0; color:var(--color-ink-soft); font-size:.72rem; line-height:1.48; }
  .dossier-detail ul { display:grid; gap:.22rem; padding-left:1.1rem; }
  .dossier-detail dl { display:grid; gap:.42rem; }
  .dossier-detail dl div { display:grid; gap:.08rem; }
  .dossier-detail dt { font-weight:700; }
  .dossier-detail dd { margin:0; color:var(--color-ink-muted); }
  .dossier-detail small { color:var(--color-ink-muted); font-weight:400; }
  .dossier-detail a { color:var(--color-active); }
  .detail-error { color:var(--color-danger); }
  .empty { padding:.8rem; border:1px dashed var(--color-rule); text-align:center; }
  .form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.65rem; }
  label { display:grid; gap:.3rem; color:var(--color-ink-soft); font-size:.77rem; font-weight:680; }
  label small { color:var(--color-ink-muted); font-weight:400; line-height:1.42; }
  input, select, textarea { width:100%; box-sizing:border-box; border:1px solid var(--color-rule-strong); padding:.52rem .58rem; background:var(--color-surface); color:var(--color-ink); font:inherit; }
  textarea { resize:vertical; line-height:1.45; }
  .json-editor { min-height:18rem; font-family:var(--font-mono,ui-monospace,monospace); font-size:.72rem; tab-size:2; }
  .notice { display:grid; gap:.2rem; padding:.65rem .72rem; border-left:3px solid var(--color-clay); background:var(--color-surface-muted); color:var(--color-ink-muted); font-size:.74rem; line-height:1.45; }
  .notice strong { color:var(--color-clay); }
  .actions { display:flex; justify-content:flex-end; gap:.45rem; }
  .btn { min-height:2.2rem; border:1px solid var(--color-rule); padding:.48rem .72rem; background:var(--color-surface); color:var(--color-ink); cursor:pointer; font:inherit; font-size:.77rem; font-weight:650; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .alert { grid-column:1/-1; padding:.65rem .75rem; border:1px solid transparent; font-size:.82rem; }
  .alert.error { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); }
  .reader-notice { display:grid; gap:.35rem; padding:.9rem; border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink-muted); font-size:.78rem; line-height:1.5; }
  .reader-notice strong { color:var(--color-ink-soft); }
  .reader-notice p { margin:0; }
  @media (max-width:820px) { .dossier-shell, .form-grid { grid-template-columns:1fr; } }
</style>
