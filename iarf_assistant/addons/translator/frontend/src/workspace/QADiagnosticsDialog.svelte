<script>
  import { Dialog } from "@carlina/ui";

  export let open = false;
  export let loading = false;
  export let loadingMore = false;
  export let error = "";
  export let payload = null;
  export let onclose = () => {};
  export let onrefresh = () => {};
  export let onloadmore = () => {};
  export let onnavigate = () => {};
  export let grouping = "token";
  export let groupId = null;
  export let ongrouping = () => {};
  export let ongroup = () => {};
  export let dispositions = "open";
  export let ondispositions = () => {};
  export let canDecide = false;
  export let decidingId = "";
  export let ondecide = async () => false;

  const BASIS_LABELS = {
    false_positive: "False positive",
    intended: "Intended"
  };

  let deciding = null;
  let basis = "false_positive";
  let reason = "";

  function startDismiss(finding) {
    deciding = finding.id;
    basis = "false_positive";
    reason = "";
  }

  async function confirmDismiss(finding) {
    if (!reason.trim()) return;
    const saved = await ondecide(finding, { action: "dismiss", basis, reason: reason.trim() });
    if (saved) deciding = null;
  }

  $: items = Array.isArray(payload?.occurrences)
    ? payload.occurrences.map((occurrence) => ({ ...(payload.groups?.[0] || {}), ...occurrence }))
    : payload?.summary?.grouping === "token"
      ? (payload.groups || []).map((group) => ({ ...group, ...(group.preview_occurrences?.[0] || {}), id: group.id, group_id: group.id }))
      : Array.isArray(payload?.items) ? payload.items : [];
  $: summary = payload?.summary || {};
  $: pageUrls = payload?.source_page_urls || {};
  $: spell = payload?.capabilities?.spellcheck || {};
  $: pageTotal = payload?.pagination?.total ?? summary.total ?? 0;
</script>

<Dialog
  {open}
  title="Translation QA"
  description="Checks structure, numbers, brackets and quotation marks, references, terminology and spelling and, where sentences are aligned, sentences much shorter than the rest of their paragraph. Names, negation and meaning still need comparison with the source. Findings do not replace a review decision."
  onclose={onclose}
>
  <div class="qa-toolbar">
    <div class="qa-summary" aria-live="polite">
      {#if payload}
        <span><strong>{summary.total || 0}</strong> {summary.truncated ? "retained findings" : "findings"}</span>
        <span>Showing {items.length} of {pageTotal} {summary.grouping === "token" && !groupId ? "groups" : groupId ? "occurrences" : "findings"}</span>
        <span>{summary.by_severity?.integrity || 0} integrity</span>
        <span>{summary.by_severity?.warning || 0} warnings</span>
        <span>{summary.by_severity?.advisory || 0} advisory</span>
        {#if summary.dismissed_total}<span>{summary.dismissed_total} dismissed</span>{/if}
      {:else}
        <span>Not scanned yet.</span>
      {/if}
    </div>
    <label>Show
      <select value={grouping} disabled={loading || loadingMore} on:change={(event) => ongrouping(event.currentTarget.value)}>
        <option value="token">Repeated tokens grouped</option>
        <option value="finding">Individual findings</option>
      </select>
    </label>
    <label>Findings
      <select value={dispositions} disabled={loading || loadingMore} on:change={(event) => ondispositions(event.currentTarget.value)}>
        <option value="open">Open</option>
        <option value="dismissed">Dismissed</option>
        <option value="all">All</option>
      </select>
    </label>
    <button type="button" class="qa-refresh" disabled={loading || loadingMore} on:click={onrefresh}>
      {loading ? "Scanning…" : "Run QA again"}
    </button>
  </div>
  {#if groupId}<button type="button" class="qa-refresh" disabled={loading || loadingMore} on:click={() => ongrouping(grouping)}>Back to all groups</button>{/if}

  {#if payload && !spell.available}
    <p class="qa-capability">Spellchecking is unavailable for {spell.requested_language || "the target language"} on this installation. Other QA checks still ran.</p>
  {/if}
  {#if payload && summary.alignment_coverage === "unavailable"}
    <p class="qa-capability">Sentence alignment could not be read, so sentence groups were not compared with the rest of their paragraph. Other QA checks still ran.</p>
  {/if}
  {#if summary.truncated}
    <p class="qa-capability qa-truncated">{summary.omitted_total || 0} additional findings were omitted at the safety cap. Narrow the category or block scope through MCP for a complete focused pass.</p>
  {/if}

  {#if error}
    <div class="qa-error" role="alert">{error}</div>
  {/if}

  {#if loading && !payload}
    <p class="qa-empty">Scanning active translations…</p>
  {:else if payload && !items.length}
    <p class="qa-empty">{dispositions === "dismissed" ? "No dismissed findings for the current translations." : "No findings under the current QA rules."}</p>
  {:else}
    <div class="qa-findings">
      {#each items as finding (finding.id)}
        <article class="qa-finding severity-{finding.severity}">
          <header>
            <div>
              <span class="qa-severity">{finding.severity}</span>
              <strong>{finding.message || finding.token || finding.code}</strong>
              {#if finding.group_id}<span>{finding.count} occurrence{finding.count === 1 ? "" : "s"}</span>{/if}
            </div>
            {#if finding.group_id}
              <button type="button" class="qa-block-link" on:click={() => ongroup(finding.group_id)}>Inspect occurrences</button>
            {:else}
              <button type="button" class="qa-block-link" on:click={() => onnavigate(finding)}>Block {finding.order}</button>
            {/if}
          </header>
          <p class="qa-code">{finding.code} · {finding.category}</p>
          {#if finding.target?.text}
            <div class="qa-excerpt">
              <span>Target</span>
              <q>{finding.target.text}</q>
            </div>
          {/if}
          {#if finding.source?.text}
            <div class="qa-excerpt">
              <span>Source</span>
              <q>{finding.source.text}</q>
            </div>
          {/if}
          {#if finding.source_pages?.length}
            <div class="qa-pages">
              <span>Physical source:</span>
              {#each finding.source_pages as page}
                {#if pageUrls[String(page)]}
                  <a href={pageUrls[String(page)]} target="_blank" rel="noopener noreferrer">page {page}</a>
                {:else}
                  <span>page {page}</span>
                {/if}
              {/each}
            </div>
          {/if}
          {#if finding.evidence?.preferred_target}
            <p class="qa-evidence">
              Preferred: <strong>{finding.evidence.preferred_target}</strong>
              {#if finding.evidence.accepted_target_forms?.length}
                · accepted here: {finding.evidence.accepted_target_forms.join(", ")}
              {/if}
            </p>
          {/if}
          {#if finding.evidence?.suggestions?.length}
            <p class="qa-evidence">Suggestions: {finding.evidence.suggestions.join(", ")}</p>
          {/if}
          {#if finding.disposition?.state === "dismissed"}
            <p class="qa-decision">Dismissed as {(BASIS_LABELS[finding.disposition.basis] || "reviewed").toLowerCase()}: {finding.disposition.reason}</p>
          {/if}
          {#if canDecide && finding.block_id && !(finding.group_id && !groupId)}
            {#if deciding === finding.id}
              <form class="qa-dismiss" on:submit|preventDefault={() => confirmDismiss(finding)}>
                <fieldset disabled={Boolean(decidingId)}>
                  <legend>Why dismiss this finding?</legend>
                  <label><input type="radio" bind:group={basis} value="false_positive" /> False positive: the check misread the text</label>
                  <label><input type="radio" bind:group={basis} value="intended" /> Intended: the translation departs on purpose</label>
                  <label class="qa-reason">Reason, kept with the finding
                    <input type="text" bind:value={reason} maxlength="500" required />
                  </label>
                </fieldset>
                <p class="qa-hint">A dismissal holds for this version of the block. If an edit brings the finding back, it shows again.</p>
                <div class="qa-actions">
                  <button type="button" class="qa-refresh" disabled={Boolean(decidingId)} on:click={() => (deciding = null)}>Cancel</button>
                  <button type="submit" class="qa-refresh qa-primary" disabled={Boolean(decidingId) || !reason.trim()}>{decidingId === finding.id ? "Saving…" : "Dismiss finding"}</button>
                </div>
              </form>
            {:else}
              <div class="qa-actions">
                {#if finding.disposition?.state === "dismissed"}
                  <button type="button" class="qa-refresh" disabled={Boolean(decidingId)} on:click={() => ondecide(finding, { action: "reopen" })}>{decidingId === finding.id ? "Saving…" : "Reopen"}</button>
                {:else}
                  <button type="button" class="qa-refresh" disabled={Boolean(decidingId)} on:click={() => startDismiss(finding)}>Dismiss…</button>
                {/if}
              </div>
            {/if}
          {/if}
        </article>
      {/each}
      {#if payload?.pagination?.next_cursor}
        <button type="button" class="qa-load-more" disabled={loadingMore} on:click={onloadmore}>
          {loadingMore ? "Loading more…" : `Load more (${items.length} of ${pageTotal})`}
        </button>
      {/if}
    </div>
  {/if}
</Dialog>

<style>
  .qa-toolbar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.8rem; margin-bottom:.8rem; }
  .qa-summary { display:flex; flex-wrap:wrap; gap:.45rem; color:var(--color-ink-soft); font-size:.82rem; }
  .qa-summary span { border:1px solid var(--color-rule); background:var(--color-paper-muted); border-radius:999px; padding:.2rem .48rem; }
  .qa-refresh { border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); border-radius:.45rem; padding:.42rem .65rem; cursor:pointer; }
  .qa-refresh:disabled { opacity:.55; cursor:not-allowed; }
  .qa-capability { margin:.2rem 0 .8rem; color:var(--color-ink-muted); font-size:.82rem; }
  .qa-capability.qa-truncated { color:var(--color-warning); }
  .qa-error { border:1px solid color-mix(in srgb,var(--color-danger) 42%,var(--color-rule)); background:var(--color-danger-muted); color:var(--color-danger); padding:.65rem; border-radius:.5rem; margin-bottom:.8rem; }
  .qa-empty { color:var(--color-ink-muted); padding:1rem 0; }
  .qa-findings { display:flex; flex-direction:column; gap:.65rem; max-height:min(65vh,48rem); overflow:auto; padding-right:.2rem; }
  .qa-finding { border:1px solid var(--color-rule); border-left-width:4px; border-radius:.52rem; background:var(--color-surface); padding:.72rem .78rem; }
  .qa-finding.severity-integrity { border-left-color:var(--color-danger); }
  .qa-finding.severity-warning { border-left-color:var(--color-warning); }
  .qa-finding.severity-advisory { border-left-color:var(--color-active); }
  .qa-finding header { display:flex; flex-wrap:wrap; align-items:flex-start; justify-content:space-between; gap:.7rem; }
  .qa-finding header > div { display:flex; flex-wrap:wrap; min-width:0; align-items:flex-start; gap:.5rem; }
  .qa-finding header strong { overflow-wrap:anywhere; }
  .qa-severity { text-transform:uppercase; font-size:.65rem; letter-spacing:.07em; color:var(--color-ink-muted); padding-top:.12rem; }
  .qa-block-link { border:0; background:transparent; color:var(--color-active); cursor:pointer; font-weight:600; white-space:nowrap; }
  .qa-code { margin:.32rem 0 .5rem; color:var(--color-ink-muted); font: .72rem/1.35 ui-monospace,SFMono-Regular,Consolas,monospace; }
  .qa-excerpt { display:grid; grid-template-columns:4.3rem 1fr; gap:.5rem; margin:.28rem 0; font-size:.84rem; }
  .qa-excerpt > span { color:var(--color-ink-muted); font-weight:600; }
  .qa-excerpt q { quotes:none; word-break:break-word; }
  .qa-pages { display:flex; flex-wrap:wrap; gap:.45rem; margin-top:.48rem; font-size:.8rem; color:var(--color-ink-muted); }
  .qa-pages a { color:var(--color-active); }
  .qa-load-more { align-self:center; border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-active); border-radius:.45rem; padding:.48rem .75rem; cursor:pointer; font-weight:650; }
  .qa-load-more:disabled { opacity:.55; cursor:not-allowed; }
  .qa-evidence { margin:.42rem 0 0; font-size:.8rem; color:var(--color-ink-soft); }
  .qa-decision { margin:.45rem 0 0; font-size:.8rem; color:var(--color-ink-muted); overflow-wrap:anywhere; }
  .qa-actions { display:flex; justify-content:flex-end; flex-wrap:wrap; gap:.45rem; margin-top:.55rem; }
  .qa-primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .qa-dismiss { margin-top:.6rem; padding:.6rem; border:1px solid var(--color-rule); background:var(--color-paper-muted); border-radius:.45rem; }
  .qa-dismiss fieldset { display:grid; gap:.35rem; margin:0; padding:0; border:0; font-size:.82rem; }
  .qa-dismiss legend { margin-bottom:.3rem; font-weight:650; color:var(--color-ink-soft); }
  .qa-dismiss label { display:flex; align-items:baseline; gap:.4rem; }
  .qa-dismiss .qa-reason { display:grid; gap:.3rem; margin-top:.25rem; }
  .qa-dismiss input[type="text"] { border:1px solid var(--color-rule-strong); padding:.42rem .5rem; background:var(--color-surface); color:var(--color-ink); font:inherit; border-radius:.35rem; }
  .qa-hint { margin:.45rem 0 0; font-size:.76rem; color:var(--color-ink-muted); }
</style>
