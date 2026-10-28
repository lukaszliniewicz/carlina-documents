<script>
  import { formatCurrency, formatInteger } from "../lib/utils.js";

  export let project = {};
  export let guestMode = false;
  export let sourceExtractionMetrics = "";
  export let metrics = { tokens: 0, cost: 0 };
  export let setupMode = false;
  export let actionBusy = false;
  export let translatingMode = false;
  export let settingsBusy = false;
  export let isSubtitleProject = false;
  export let syncStatus = "All changes saved";
  export let syncStatusTone = "ok";
  export let onRunAnalyzer = () => {};
  export let onToggleTranslateAll = () => {};
  export let onShare = () => {};
  export let onSettings = () => {};
  export let onPreview = () => {};
  export let onExport = () => {};
</script>

<header class="workspace-header">
  <div class="header-main">
    <div class="title-wrap">
      {#if !guestMode}
        <a href="/addons/translator/" class="back-link">Back to Projects</a>
      {/if}
      <h1>{project.name}</h1>
      <p>{project.source_lang} to {project.target_lang}</p>
    </div>
    <div class="metric-row">
      {#if sourceExtractionMetrics}
        <span class="metric-chip">Source extraction: {sourceExtractionMetrics}</span>
      {/if}
      <span class="metric-chip strong">Tokens: {formatInteger(metrics.tokens)} | Cost: ${formatCurrency(metrics.cost)}</span>
    </div>
  </div>

  <div class="header-controls">
    <div class="status-group">
      <span class="status-label">Sync</span>
      <span class={`sync-badge ${syncStatusTone}`}>{syncStatus}</span>
    </div>
    <div class="header-actions">
      {#if setupMode}
        <button class="btn warning" type="button" on:click={onRunAnalyzer} disabled={actionBusy || project.status === "analyzing_terminology"}>
          {actionBusy || project.status === "analyzing_terminology" ? "Building Glossary..." : "Run Terminology Analyzer"}
        </button>
        <button class="btn success" type="button" on:click={onToggleTranslateAll} disabled={actionBusy || project.status === "analyzing_terminology"}>
          Skip and Start Translation
        </button>
      {:else}
        {#if !guestMode}
          <button class="btn" type="button" on:click={onShare}>Share</button>
          <button class="btn" type="button" on:click={onSettings} disabled={settingsBusy}>Settings</button>
        {/if}
        <button class="btn" type="button" on:click={onPreview}>Preview</button>
        <button class={`btn ${translatingMode ? "danger" : "primary"}`} type="button" on:click={onToggleTranslateAll} disabled={guestMode || actionBusy}>
          {actionBusy ? "Updating..." : translatingMode ? "Stop Translation" : "AI Translate Remaining"}
        </button>
        <button class="btn primary" type="button" on:click={onExport}>
          {isSubtitleProject ? "Export Subtitles" : "Export Document"}
        </button>
      {/if}
    </div>
  </div>
</header>

<style>
  .workspace-header { display:flex; justify-content:space-between; align-items:flex-end; gap:1rem; border-bottom:1px solid var(--color-rule); background:color-mix(in srgb,var(--color-surface) 95%,var(--color-paper)); padding:.95rem 1rem .85rem; }
  .header-main { min-width:0; display:flex; flex-direction:column; gap:.55rem; }
  .title-wrap { display:flex; flex-direction:column; gap:.28rem; }
  .title-wrap h1 { margin:0; font-size:1.26rem; line-height:1.25; letter-spacing:-.01em; }
  .title-wrap p { margin:0; color:var(--color-ink-muted); font-size:.89rem; }
  .back-link { color:var(--color-active); text-decoration:none; font-weight:600; font-size:.83rem; width:fit-content; }
  .back-link:hover { text-decoration:underline; text-decoration-thickness:1px; text-underline-offset:.12rem; }
  .metric-row { display:flex; flex-wrap:wrap; gap:.45rem; }
  .metric-chip { width:fit-content; padding:.22rem .56rem; border:1px solid color-mix(in srgb,var(--color-active) 18%,var(--color-rule)); border-radius:999px; background:var(--color-paper-muted); color:var(--color-ink-soft); font-size:.77rem; }
  .metric-chip.strong { font-weight:600; border-color:color-mix(in srgb,var(--color-active) 28%,var(--color-rule)); color:var(--color-active); }
  .header-controls { display:flex; flex-direction:column; flex-wrap:wrap; align-items:flex-end; gap:.5rem; justify-content:center; }
  .status-group { display:flex; align-items:center; gap:.45rem; }
  .status-label { font-size:.74rem; letter-spacing:.08em; text-transform:uppercase; color:var(--color-ink-muted); }
  .header-actions { display:flex; flex-wrap:wrap; gap:.42rem; justify-content:flex-end; }
  .sync-badge { font-size:.76rem; padding:.26rem .6rem; border:1px solid var(--color-rule); border-radius:999px; background:var(--color-paper-muted); color:var(--color-ink-soft); }
  .sync-badge.ok { border-color:color-mix(in srgb,var(--color-active) 30%,var(--color-rule)); color:var(--color-active); background:var(--color-active-muted); }
  .sync-badge.busy { border-color:color-mix(in srgb,var(--color-warning) 40%,var(--color-rule)); color:var(--color-warning); background:var(--color-warning-muted); }
  .sync-badge.error { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); color:var(--color-danger); background:var(--color-danger-muted); }
  .btn { border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); padding:.46rem .76rem; cursor:pointer; font-size:.86rem; border-radius:.48rem; }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .btn.primary,.btn.success { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .btn.warning { border-color:var(--color-warning); background:var(--color-warning); color:var(--color-surface); }
  .btn.danger { border-color:var(--color-danger); background:var(--color-danger); color:var(--color-surface); }
  @media (max-width:1280px) { .workspace-header { align-items:stretch; } .header-controls { align-items:flex-start; gap:.65rem; } .header-actions { justify-content:flex-start; } }
  @media (max-width:760px) { .workspace-header { flex-direction:column; align-items:stretch; } .header-actions { width:100%; justify-content:stretch; } .header-actions button { flex:1 1 auto; } }
</style>
