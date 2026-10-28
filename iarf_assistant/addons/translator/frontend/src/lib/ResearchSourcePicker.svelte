<script>
  import {
    groupBlockResearchToolOptions,
    researchToolSelectionKey
  } from "./research-providers.js";

  export let options = [];
  export let disabled = false;
  export let onToggle = () => {};

  $: groups = groupBlockResearchToolOptions(options);

  function optionKey(option) {
    return option.selection
      ? researchToolSelectionKey(option.selection)
      : `${option.addon_id}:${option.provider_domain}:${option.provider_id}:${option.tool_id}:unavailable`;
  }
</script>

<div class="research-sources">
  <div class="sources-heading">
    <div>
      <h3>Research sources</h3>
      <p class="hint">Choose what the researcher may read. These choices do not grant write access.</p>
    </div>
  </div>

  <section class="source-section recommended">
    <header>
      <div>
        <h4>Web</h4>
        <p>General live search and page reading. Jina-first search and Jina Reader are the ordinary text defaults.</p>
      </div>
      <span>Recommended</span>
    </header>
    {#if groups.web.length}
      <div class="tool-list">
        {#each groups.web as option (optionKey(option))}
          <label class="tool-option">
            <input
              type="checkbox"
              checked={Boolean(option.selected)}
              disabled={disabled || !option.selection || option.selectionConflict || (option.unavailable && !option.selected)}
              aria-label={`${option.tool_name} — ${option.provider_name}`}
              on:change={(event) => onToggle(option, event)}
            />
            <span>
              <strong>{option.tool_name}</strong>
              <small>{option.provider_name} · {option.connection_label}</small>
              {#if option.selectionConflict}<small class="warning">Only one connection may be selected for this tool.</small>{/if}
              {#if option.unavailable}<em>Unavailable — uncheck this saved selection before enabling research.</em>{/if}
            </span>
          </label>
        {/each}
      </div>
    {:else}
      <p class="hint section-empty">Activate Web Search or Scraping to make live-web research available.</p>
    {/if}
  </section>

  <section class="source-section">
    <header>
      <div>
        <h4>Library</h4>
        <p>Search vector-ready Knowledge collections for trusted background and prior material.</p>
      </div>
      <a href="/knowledge/" target="_blank" rel="noopener">Manage Knowledge</a>
    </header>
    {#if groups.library.length}
      <div class="tool-list">
        {#each groups.library as option (optionKey(option))}
          <label class="tool-option">
            <input
              type="checkbox"
              checked={Boolean(option.selected)}
              disabled={disabled || !option.selection || option.selectionConflict || (option.unavailable && !option.selected)}
              aria-label={`${option.tool_name} — ${option.provider_name}`}
              on:change={(event) => onToggle(option, event)}
            />
            <span>
              <strong>{option.tool_name}</strong>
              <small>{option.provider_name} · {option.connection_label}</small>
              {#if option.private_data_capable || option.authority_warning}<small class="warning">May read private collection data through this selected capability.</small>{/if}
              {#if option.unavailable}<em>Unavailable — uncheck this saved selection before enabling research.</em>{/if}
            </span>
          </label>
        {/each}
      </div>
    {:else}
      <p class="hint section-empty">Collection selection is not connected to this workflow yet. You can prepare and manage collections in Knowledge now.</p>
    {/if}
  </section>

  <details class="advanced-sources" open={groups.advanced.some((option) => option.selected)}>
    <summary>Advanced sources <span>{groups.advanced.length}</span></summary>
    <p class="hint">Other installed read-only tools. Technical identity remains visible here so saved connections are auditable.</p>
    {#if groups.advanced.length}
      <div class="tool-list">
        {#each groups.advanced as option (optionKey(option))}
          <label class="tool-option">
            <input
              type="checkbox"
              checked={Boolean(option.selected)}
              disabled={disabled || !option.selection || option.selectionConflict || (option.unavailable && !option.selected)}
              aria-label={`${option.tool_name} — ${option.provider_name}`}
              on:change={(event) => onToggle(option, event)}
            />
            <span>
              <strong>{option.tool_name} · {option.provider_name}</strong>
              <small>{option.addon_id} · {option.provider_domain} · {option.connection_label}</small>
              {#if option.selectionConflict}<small class="warning">Only one exact connection may be selected for this tool.</small>{/if}
              {#if option.private_data_capable || option.authority_warning}<small class="warning">May access private or connected data; only the selected read-only capability is allowed.</small>{/if}
              {#if option.unavailable}<em>Unavailable — uncheck this saved selection before enabling research.</em>{/if}
            </span>
          </label>
        {/each}
      </div>
    {:else}
      <p class="hint section-empty">No additional read-only tools are available.</p>
    {/if}
  </details>
</div>

<style>
  .research-sources { display:grid; gap:.6rem; }
  .sources-heading { display:flex; align-items:start; justify-content:space-between; gap:1rem; }
  h3, h4 { margin:0; color:var(--color-ink-soft); }
  h3 { font-size:.92rem; }
  h4 { font-size:.86rem; }
  .hint, small { margin:0; color:var(--color-ink-muted); font-size:.78rem; line-height:1.4; }
  .source-section { display:grid; gap:.5rem; padding:.7rem; border:1px solid var(--color-rule); border-radius:.45rem; background:var(--color-surface); }
  .source-section.recommended { border-color:color-mix(in srgb,var(--color-active) 30%,var(--color-rule)); }
  .source-section header { display:flex; align-items:start; justify-content:space-between; gap:1rem; }
  .source-section header p { margin:.18rem 0 0; color:var(--color-ink-muted); font-size:.76rem; line-height:1.4; }
  .source-section header > span { padding:.15rem .38rem; border-radius:999px; background:var(--color-active-muted); color:var(--color-active); font-size:.64rem; font-weight:700; text-transform:uppercase; }
  .source-section header a { color:var(--color-active); font-size:.74rem; font-weight:650; white-space:nowrap; }
  .tool-list { display:grid; gap:.35rem; margin:0; }
  .tool-option { display:grid; grid-template-columns:auto minmax(0,1fr); gap:.55rem; align-items:start; padding:.35rem; color:var(--color-ink-soft); font-size:.9rem; }
  .tool-option input { width:1rem; height:1rem; margin:.1rem 0 0; padding:0; accent-color:var(--color-active); }
  .tool-option > span { display:grid; gap:.15rem; }
  .tool-option em { width:fit-content; color:var(--color-danger); font-size:.75rem; font-style:normal; }
  .warning { color:var(--color-warning,#9a6700); }
  .section-empty { padding:.25rem .35rem; }
  .advanced-sources { padding:.65rem .7rem; border:1px solid var(--color-rule); border-radius:.45rem; }
  .advanced-sources summary { display:flex; justify-content:space-between; gap:1rem; color:var(--color-ink-soft); cursor:pointer; font-size:.82rem; font-weight:650; }
  .advanced-sources summary span { color:var(--color-ink-muted); font-weight:400; }
  .advanced-sources > .hint { margin:.5rem 0; }
</style>
