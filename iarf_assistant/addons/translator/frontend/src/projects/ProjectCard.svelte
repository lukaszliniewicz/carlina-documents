<script>
  import { ACTIVE_PROJECT_STATUSES, formatStatus } from "../lib/utils.js";

  export let project;
  export let modelOptions = [];
  export let onOpen = () => {};
  export let onDelete = () => {};

  $: tone = statusTone(project?.status);

  function statusTone(status) {
    if (ACTIVE_PROJECT_STATUSES.has(status)) return "is-active";
    if (["cancelled", "error", "failed", "partial"].includes(status)) return "is-warning";
    if (["completed", "ready"].includes(status)) return "is-success";
    return "is-neutral";
  }

  function formatDate(value) {
    if (!value) return "Unknown";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
  }

  function modelName(modelId) {
    const id = String(modelId || "").trim();
    const found = modelOptions.find((model) => model.id === id);
    return found?.name || id.split("/").pop()?.replace(/[-_]/g, " ") || "Not configured";
  }

  function sourceLabel() {
    return project?.source_type === "subtitle_studio" ? "Subtitle source" : "Document source";
  }

  function sourceName() {
    if (project?.source_type === "subtitle_studio") return project?.subtitle_project?.name || "Unavailable";
    return project?.document?.filename || "Unavailable";
  }

  function sourceStatus() {
    if (project?.source_status === "capability_setup_required") return "Setup required";
    if (project?.source_status === "unavailable") return "Source unavailable";
    if (project?.source_type === "subtitle_studio") return project?.subtitle_project ? "Subtitle source ready" : "Source unavailable";
    return project?.document_status ? `Document ${formatStatus(project.document_status)}` : "Source unavailable";
  }

  function actionLabel() {
    if (project?.status === "analyzing_terminology") return "Building Glossary · Open";
    if (ACTIVE_PROJECT_STATUSES.has(project?.status)) return "Processing · Open";
    return "Open Workspace";
  }

  function formatProgress(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "";
    return `${Math.round(Math.max(0, Math.min(100, numeric)))}%`;
  }
</script>

<article class={`project-card ${tone}`} data-project-id={project?.id}>
  <div class="project-summary">
    <div class="project-mark" aria-hidden="true">{String(project?.name || "T").trim().charAt(0).toUpperCase() || "T"}</div>
    <div class="project-title-group">
      <span class={`status ${tone}`}>{formatStatus(project?.status) || "Unknown"}</span>
      <h2>{project?.name}</h2>
      <p class="direction"><span>{project?.source_lang}</span><span aria-hidden="true">→</span><span>{project?.target_lang}</span></p>
    </div>
  </div>

  <div class="project-details">
    <div class="document-row">
      <p class="meta-item source-document"><span class="meta-label">{sourceLabel()}</span><span class="meta-value ellipsis" title={sourceName()}>{sourceName()}</span></p>
      <span class="document-status">{sourceStatus()}</span>
    </div>
    <div class="metadata-grid">
      <p class="meta-item"><span class="meta-label">Last updated</span><span class="meta-value">{formatDate(project?.updated_at)}</span></p>
      <p class="meta-item"><span class="meta-label">Translator</span><span class="meta-value">{modelName(project?.settings?.translator_model)}</span></p>
      <p class="meta-item"><span class="meta-label">Chunking</span><span class="meta-value">{Number(project?.settings?.chunk_size_chars) > 0 ? `${Number(project.settings.chunk_size_chars).toLocaleString()} chars` : "Default"}</span></p>
    </div>
    <div class="chip-row">
      <span class="chip">{project?.settings?.enable_pre_analysis ? "Terminology analysis" : "No pre-analysis"}</span>
      <span class="chip">Research: {project?.settings?.enable_research ? modelName(project?.settings?.researcher_model) : "Off"}</span>
      <span class="chip">Created {formatDate(project?.created_at)}</span>
      {#if project?.latest_operation}
        <span class="chip" title={project.latest_operation.message || "Translation operation"}>
          Operation: {formatStatus(project.latest_operation.state)} {#if project.latest_operation.progress != null}({formatProgress(project.latest_operation.progress)}){/if}
        </span>
      {/if}
      {#if project?.settings?.setup_error || project?.settings?.last_job_error}
        <span class="chip chip-error" title={project.settings.setup_error || project.settings.last_job_error}>Job failed: {project.settings.setup_error || project.settings.last_job_error}</span>
      {/if}
      {#if project?.source_error?.error}<span class="chip chip-error" title={project.source_error.error}>{project.source_error.error}</span>{/if}
    </div>
  </div>

  <div class="actions">
    <button class="btn btn-primary" type="button" on:click={() => onOpen(project)}>{actionLabel()}</button>
    <button class="btn btn-danger btn-compact" type="button" on:click={() => onDelete(project)}>Delete</button>
  </div>
</article>

<style>
  .project-card { position: relative; border: 1px solid var(--color-rule, #d0c8bc); background: var(--color-surface, #faf8f3); padding: 1rem; display: grid; grid-template-columns: minmax(210px,.9fr) minmax(320px,2fr) minmax(138px,auto); gap: 1.2rem; align-items: center; overflow: hidden; }
  .project-card::before { content: ""; position:absolute; inset:0 auto 0 0; width:3px; background:var(--color-rule,#ddd); }
  .project-card.is-active::before { background:var(--color-active,#356ae6); }
  .project-card.is-success::before { background:#1e7a3d; }
  .project-card.is-warning::before { background:var(--color-danger,#b33); }
  .project-summary { display:flex; align-items:center; gap:.8rem; min-width:0; }
  .project-mark { width:2.65rem; height:2.65rem; flex:0 0 2.65rem; display:grid; place-items:center; border:1px solid var(--color-rule-strong,#968b7d); color:var(--color-clay,#7c6053); background:var(--color-surface-muted,#e9e4db); font-family:var(--font-editorial,Georgia,serif); font-size:1.15rem; font-weight:600; }
  .project-title-group { min-width:0; }
  h2 { margin:.35rem 0 0; font-family:var(--font-editorial,Georgia,serif); font-size:1.15rem; font-weight:500; line-height:1.25; overflow-wrap:anywhere; }
  .direction { margin:.3rem 0 0; color:var(--color-ink-muted,#667); font-size:.84rem; display:flex; gap:.35rem; }
  .status { display:inline-flex; width:fit-content; min-height:1.45rem; align-items:center; font-size:.62rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--color-ink-soft,#4d4842); background:var(--color-surface-muted,#e9e4db); border:1px solid var(--color-rule,#d0c8bc); padding:.18rem .4rem; white-space:nowrap; }
  .status.is-active { color:var(--color-tobacco,#69583f); background:var(--color-tobacco-muted,#e4ded2); }
  .status.is-warning { color:var(--color-danger,#704745); background:var(--color-danger-muted,#e8d9d7); }
  .status.is-success { color:var(--color-lichen,#56604f); background:var(--color-lichen-muted,#dce0d8); }
  .project-details { min-width:0; padding-left:1.2rem; border-left:1px solid var(--color-rule,#ddd); display:flex; flex-direction:column; gap:.7rem; }
  .document-row { min-width:0; display:flex; align-items:end; gap:.8rem; }
  .source-document { min-width:0; flex:1; }
  .document-status { flex-shrink:0; color:var(--color-ink-muted,#667); font-size:.72rem; }
  .metadata-grid { display:grid; gap:.65rem .95rem; grid-template-columns:repeat(3,minmax(0,1fr)); }
  .meta-item { margin:0; display:flex; flex-direction:column; gap:.15rem; min-width:0; }
  .meta-label { text-transform:uppercase; letter-spacing:.04em; color:var(--color-ink-muted,#667); font-size:.65rem; }
  .meta-value { color:var(--color-ink-soft,#555); font-size:.86rem; line-height:1.35; }
  .ellipsis { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .chip-row { display:flex; gap:.4rem; flex-wrap:wrap; }
  .chip { font-size:.68rem; color:var(--color-ink-muted,#675f56); background:var(--color-surface-muted,#e9e4db); border:1px solid var(--color-rule,#d0c8bc); padding:.15rem .42rem; }
  .chip-error { color:var(--color-danger,#b33); background:var(--color-danger-muted,#fff0f0); }
  .actions { align-self:stretch; padding-left:1.2rem; border-left:1px solid var(--color-rule,#ddd); display:flex; flex-direction:column; justify-content:center; gap:.5rem; }
  .btn { min-height:2.1rem; border:1px solid transparent; padding:.48rem .68rem; cursor:pointer; font:inherit; font-size:.73rem; font-weight:650; letter-spacing:.025em; }
  .btn-primary { background:var(--color-umber,#5c5149); border-color:var(--color-umber,#5c5149); color:var(--color-surface,#faf8f3); }
  .btn-danger { background:var(--color-danger-muted,#fff0f0); border-color:color-mix(in srgb,var(--color-danger,#b33) 35%,var(--color-rule,#ddd)); color:var(--color-danger,#b33); }
  .btn-compact { padding-block:.4rem; font-size:.82rem; }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .actions .btn { width:100%; white-space:nowrap; }
  @media (max-width: 760px) { .project-card { grid-template-columns:1fr; } .project-details,.actions { border-left:0; padding-left:0; } .actions { flex-direction:row; justify-content:flex-start; } .actions .btn { width:auto; } }
</style>
