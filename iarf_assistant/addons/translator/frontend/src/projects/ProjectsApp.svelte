<script>
  import { onDestroy, onMount } from "svelte";
  import { createApiClient, createIdempotencyKey, isAbortError } from "../lib/api.js";
  import { createAbortController } from "../lib/request-state.js";
  import {
    allSelectedResearchToolsExecutable,
    blockResearchToolOptions,
    normaliseBlockResearchTools,
    normaliseResearchProviders,
    recommendedResearchToolSelections,
    researchProviderOptions,
    researchToolSelectionKey,
    selectedResearchProviderIds,
    selectedResearchToolSelections
  } from "../lib/research-providers.js";
  import { ACTIVE_PROJECT_STATUSES, modelSettingsHref, modelSetupMessage, normaliseModels, validateProjectStatusPayload } from "../lib/utils.js";
  import ProjectCard from "./ProjectCard.svelte";
  import ProjectCreateDialog from "./ProjectCreateDialog.svelte";
  import { buildProjectCreatePayload, retainCreateRequestKey } from "./create-request.js";
  import { defaultSourceLanguage } from "./source-language.js";
  import ConfirmationDialog from "../lib/ConfirmationDialog.svelte";
  import {
    DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
    DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
    isValidTranslationBatchMaxBlocks,
    isValidTranslationBatchInputTokens
  } from "../lib/translation-batch-limits.js";

  export let context = null;

  const client = createApiClient({
    addonApi: context?.api || null,
    signal: context?.signal || null
  });
  const lifecycleController = createAbortController(context?.signal || null);

  let projects = [];
  let projectError = "";
  let isLoadingProjects = true;
  let projectPollTimer = null;

  let isNewModalOpen = false;
  let isCreatingProject = false;
  let createError = "";
  let createRequestState = null;
  let pendingDelete = null;
  let deleteBusy = false;

  let documents = [];
  let documentsLoadError = "";
  let isLoadingDocuments = false;
  let subtitleProjects = [];
  let subtitleProjectsLoadError = "";
  let isLoadingSubtitleProjects = false;

  let modelOptions = [];
  let isLoadingModels = false;
  let modelLoadError = "";
  let researchProviders = [];
  let researchProvidersLoadError = "";
  let researchTools = [];
  let researchToolsLoadError = "";
  let isLoadingResearchProviders = false;

  let selectedSourceId = "";
  let selectedSourceIsLoading = true;
  let selectedSourceIsAvailable = false;
  let canCreateProject = false;
  let sourceLanguageIsAutomatic = true;

  $: activeProjectCount = projects.filter((project) => ACTIVE_PROJECT_STATUSES.has(project.status)).length;
  $: readyProjectCount = projects.filter((project) => ["completed", "ready"].includes(project.status)).length;
  $: attentionProjectCount = projects.filter((project) => ["cancelled", "error", "failed", "partial"].includes(project.status)).length;

  $: modelOptionsByProvider = modelOptions.reduce((acc, model) => {
    const provider = model.provider || "Other";
    if (!acc[provider]) acc[provider] = [];
    acc[provider].push(model);
    return acc;
  }, {});

  let form = {
    name: "",
    source_type: "document",
    engine_contract: "document-block-v1",
    source_lang: "Auto-detect",
    target_lang: "",
    document_id: "",
    subtitle_project_id: "",
    enable_pre_analysis: true,
    analyzer_model: "",
    translator_model: "",
    researcher_model: "",
    enable_research: false,
    enable_translator_notes: false,
    annotation_profile: "none",
    translation_unit_mode: "block",
    translation_context_mode: "self_contained",
    research_plugin_ids: [],
    research_tool_selections: [],
    translation_batch_input_tokens: DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
    translation_batch_max_blocks: DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
    chunk_size: 2500
  };

  $: researchProviderRows = researchProviderOptions(researchProviders, form.research_plugin_ids);
  $: blockEngineSelected = form.source_type === "document";
  $: blockBatchBudgetValid = !blockEngineSelected
    || (
      isValidTranslationBatchInputTokens(form.translation_batch_input_tokens)
      && isValidTranslationBatchMaxBlocks(form.translation_batch_max_blocks)
    );
  $: researchToolOptions = blockResearchToolOptions(researchTools, form.research_tool_selections);
  $: blockResearchSelectionsExecutable = allSelectedResearchToolsExecutable(researchTools, form.research_tool_selections);
  $: researchEnabled = blockEngineSelected
    ? Boolean(form.enable_pre_analysis && form.enable_research && blockResearchSelectionsExecutable)
    : Boolean(form.enable_research && selectedResearchProviderIds(form.research_plugin_ids).length);

  $: selectedSourceId = form.source_type === "subtitle_studio"
    ? form.subtitle_project_id
    : form.document_id;
  $: selectedSourceIsLoading = form.source_type === "subtitle_studio"
    ? isLoadingSubtitleProjects
    : isLoadingDocuments;
  $: selectedSourceIsAvailable = form.source_type === "subtitle_studio"
    ? subtitleProjects.some((project) => String(project.id) === String(selectedSourceId))
    : documents.some((document) => String(document.id) === String(selectedSourceId));
  $: canCreateProject = Boolean(
    !isCreatingProject &&
    !selectedSourceIsLoading &&
    modelOptions.length > 0 &&
    selectedSourceIsAvailable &&
    form.name.trim() &&
    form.target_lang.trim() &&
    blockBatchBudgetValid &&
    (!form.enable_research || researchEnabled)
  );

  function clearPollTimer() {
    if (projectPollTimer) {
      clearTimeout(projectPollTimer);
      projectPollTimer = null;
    }
  }

  function schedulePollIfNeeded() {
    clearPollTimer();
    const hasActive = projects.some((project) => ACTIVE_PROJECT_STATUSES.has(project.status));
    if (hasActive) {
      projectPollTimer = setTimeout(loadActiveProjectStatuses, 3000);
    }
  }

  function resetCreateForm() {
    createRequestState = null;
    sourceLanguageIsAutomatic = true;
    const preferredPro = modelOptions.find((model) => model.id.toLowerCase().includes("pro")) || modelOptions[0];
    const preferredFlash = modelOptions.find((model) => model.id.toLowerCase().includes("flash")) || modelOptions[0];
    const defaultSourceType = documents.length ? "document" : (subtitleProjects.length ? "subtitle_studio" : "document");
    form = {
      name: "",
      source_type: defaultSourceType,
      engine_contract: defaultSourceType === "subtitle_studio" ? "segment-v1" : "document-block-v1",
      source_lang: "Auto-detect",
      target_lang: "",
      document_id: documents[0]?.id || "",
      subtitle_project_id: subtitleProjects[0]?.id || "",
      enable_pre_analysis: true,
      analyzer_model: preferredPro?.id || "",
      translator_model: preferredPro?.id || "",
      researcher_model: preferredFlash?.id || preferredPro?.id || "",
      enable_research: false,
      enable_translator_notes: false,
      annotation_profile: "none",
      translation_context_mode: "self_contained",
      research_plugin_ids: [],
      research_tool_selections: [],
      translation_batch_input_tokens: DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
      translation_batch_max_blocks: DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
      chunk_size: 2500
    };
  }

  function syncSourceLanguageFromDocument() {
    if (form.source_type !== "document") return;
    const source = documents.find((document) => String(document.id) === String(form.document_id));
    if (!source || (!sourceLanguageIsAutomatic && form.source_lang && form.source_lang !== "Auto-detect")) return;
    const sourceLanguage = defaultSourceLanguage(source, form.source_lang);
    if (sourceLanguage !== form.source_lang) {
      form = { ...form, source_lang: sourceLanguage };
    }
    sourceLanguageIsAutomatic = true;
  }

  $: if (form.source_type === "document" && form.document_id) {
    syncSourceLanguageFromDocument();
  }

  async function loadResearchProviders() {
    isLoadingResearchProviders = true;
    researchProvidersLoadError = "";
    researchToolsLoadError = "";
    try {
      const [legacyResult, blockResult] = await Promise.allSettled([
        client.fetchJson("/addons/translator/api/research-providers", { signal: lifecycleController.signal }),
        client.fetchJson("/addons/translator/api/block-research-tools", { signal: lifecycleController.signal })
      ]);
      if (legacyResult.status === "fulfilled") {
        researchProviders = normaliseResearchProviders(legacyResult.value);
      } else if (!isAbortError(legacyResult.reason)) {
        researchProviders = [];
        researchProvidersLoadError = legacyResult.reason?.message || "Unable to load research provider options.";
      }
      if (blockResult.status === "fulfilled") {
        researchTools = normaliseBlockResearchTools(blockResult.value);
      } else if (!isAbortError(blockResult.reason)) {
        researchTools = [];
        researchToolsLoadError = blockResult.reason?.message || "Unable to load exact research tool options.";
      }
    } catch (error) {
      if (error?.name !== "AbortError") {
        researchProvidersLoadError = error.message || "Unable to load research provider options.";
        researchProviders = [];
        researchTools = [];
      }
    } finally {
      isLoadingResearchProviders = false;
    }
  }

  async function loadModels() {
    isLoadingModels = true;
    modelLoadError = "";
    try {
      const payload = await client.fetchJson("/api/llm/models?model_type=chat&ready_only=true", { signal: lifecycleController.signal });
      const normalised = normaliseModels(payload);
      modelOptions = normalised;
    } catch (error) {
      if (isAbortError(error)) return;
      modelLoadError = error.message || "Unable to load configured model options.";
      modelOptions = [];
    } finally {
      isLoadingModels = false;
      resetCreateForm();
    }
  }

  async function loadProjects() {
    clearPollTimer();
    const showLoadingState = projects.length === 0;
    if (showLoadingState) isLoadingProjects = true;
    projectError = "";
    try {
      projects = await client.fetchJson("/addons/translator/api/projects", { signal: lifecycleController.signal });
      schedulePollIfNeeded();
    } catch (error) {
      if (isAbortError(error)) return;
      projectError = error.message || "Unable to load projects.";
      clearPollTimer();
    } finally {
      if (showLoadingState) isLoadingProjects = false;
    }
  }

  async function loadActiveProjectStatuses() {
    clearPollTimer();
    const activeProjects = projects.filter((project) => ACTIVE_PROJECT_STATUSES.has(project.status));
    if (!activeProjects.length) return;
    try {
      const statuses = await Promise.all(activeProjects.map(async (project) => {
        const payload = await client.fetchJson(
          `/addons/translator/api/projects/${encodeURIComponent(project.id)}/status`,
          { signal: lifecycleController.signal }
        );
        return validateProjectStatusPayload(payload, project.id);
      }));
      const statusById = new Map(statuses.map((status) => [String(status.project_id), status]));
      const observedTerminalState = statuses.some((status) => !ACTIVE_PROJECT_STATUSES.has(status.status));
      projects = projects.map((project) => {
        const status = statusById.get(String(project.id));
        return status
          ? { ...project, status: status.status, latest_operation: status.latest_operation }
          : project;
      });
      projectError = "";
      if (observedTerminalState) {
        await loadProjects();
      } else {
        schedulePollIfNeeded();
      }
    } catch (error) {
      if (isAbortError(error)) return;
      projectError = error.message || "Unable to refresh active project status.";
      schedulePollIfNeeded();
    }
  }

  async function openCreateModal() {
    isNewModalOpen = true;
    createError = "";
    documentsLoadError = "";
    subtitleProjectsLoadError = "";
    researchProvidersLoadError = "";
    researchToolsLoadError = "";
    isLoadingDocuments = true;
    isLoadingSubtitleProjects = true;

    resetCreateForm();

    const docsPromise = client.fetchJson("/addons/translator/api/document_sources", { signal: lifecycleController.signal })
      .then((allDocs) => {
        documents = Array.isArray(allDocs) ? allDocs.filter((document) => document.status === "completed") : [];
        if (!documents.length) {
          documentsLoadError = "No processed documents are available yet.";
        }
        if (documents.length && !form.document_id) {
          form.document_id = documents[0].id;
        }
      })
      .catch((error) => {
        documents = [];
        documentsLoadError = error.message || "Unable to load documents.";
      })
      .finally(() => {
        isLoadingDocuments = false;
      });

    const subtitlesPromise = client.fetchJson("/addons/translator/api/subtitle_sources", { signal: lifecycleController.signal })
      .then((payload) => {
        subtitleProjects = Array.isArray(payload) ? payload : [];
        if (!subtitleProjects.length) {
          subtitleProjectsLoadError = "No Subtitle Studio projects with cues are available yet.";
        }
        if (subtitleProjects.length && !form.subtitle_project_id) {
          form.subtitle_project_id = subtitleProjects[0].id;
        }
      })
      .catch((error) => {
        subtitleProjects = [];
        subtitleProjectsLoadError = error.message || "Unable to load subtitle projects.";
      })
      .finally(() => {
        isLoadingSubtitleProjects = false;
      });

    await Promise.allSettled([docsPromise, subtitlesPromise, loadResearchProviders()]);
    if (documents.length && !form.document_id) form.document_id = documents[0].id;
    if (subtitleProjects.length && !form.subtitle_project_id) form.subtitle_project_id = subtitleProjects[0].id;
  }

  function hasSelectedSource() {
    if (form.source_type === "subtitle_studio") {
      return Boolean(form.subtitle_project_id);
    }
    return Boolean(form.document_id);
  }

  function syncSourceLanguageFromSubtitle() {
    if (form.source_type !== "subtitle_studio") return;
    const source = subtitleProjects.find((project) => String(project.id) === String(form.subtitle_project_id));
    if (!source || (!sourceLanguageIsAutomatic && form.source_lang && form.source_lang !== "Auto-detect")) return;
    const sourceLanguage = defaultSourceLanguage(source, form.source_lang);
    if (sourceLanguage !== form.source_lang) {
      form = { ...form, source_lang: sourceLanguage };
    }
    sourceLanguageIsAutomatic = true;
  }

  $: if (form.source_type === "subtitle_studio" && form.subtitle_project_id) {
    syncSourceLanguageFromSubtitle();
  }

  function closeCreateModal() {
    isNewModalOpen = false;
    createError = "";
    isCreatingProject = false;
    createRequestState = null;
  }

  function toggleResearchProvider(providerId, event) {
    const selected = new Set(selectedResearchProviderIds(form.research_plugin_ids));
    if (event.currentTarget.checked) selected.add(providerId);
    else selected.delete(providerId);
    form = { ...form, research_plugin_ids: [...selected] };
  }

  function toggleResearchTool(option, event) {
    const selection = option?.selection;
    if (!selection || option.selectionConflict) return;
    const exact = selectedResearchToolSelections(form.research_tool_selections);
    const key = researchToolSelectionKey(selection);
    if (!key) return;
    const selected = new Map(exact.map((item) => [researchToolSelectionKey(item), item]));
    if (event.currentTarget.checked) selected.set(key, selection);
    else selected.delete(key);
    form = { ...form, research_tool_selections: [...selected.values()] };
  }

  function updateCreateFormField(field, value) {
    if (field === "source_lang") {
      sourceLanguageIsAutomatic = !String(value ?? "").trim() || value === "Auto-detect";
    }
    if (field === "source_type") {
      form = {
        ...form,
        source_type: value,
        engine_contract: value === "subtitle_studio" ? "segment-v1" : "document-block-v1"
      };
      return;
    }
    if (field === "enable_pre_analysis" && !value && blockEngineSelected) {
      form = { ...form, enable_pre_analysis: false, enable_research: false };
      return;
    }
    if (field === "enable_research" && value && blockEngineSelected) {
      const exact = selectedResearchToolSelections(form.research_tool_selections);
      const recommended = exact.length ? exact : recommendedResearchToolSelections(researchToolOptions);
      form = {
        ...form,
        enable_pre_analysis: true,
        enable_research: true,
        research_tool_selections: recommended
      };
      return;
    }
    form = { ...form, [field]: value };
  }

  async function handleCreateProject(event) {
    event.preventDefault();
    createError = "";

    if (!form.name.trim() || !form.target_lang.trim() || !hasSelectedSource()) {
      createError = "Project name, target language, and source are required.";
      return;
    }

    isCreatingProject = true;
    try {
      const payload = buildProjectCreatePayload(form, researchEnabled);
      const requestKey = retainCreateRequestKey(
        createRequestState,
        payload,
        () => createIdempotencyKey("translator.project.create")
      );
      createRequestState = requestKey.state;
      await client.fetchJson("/addons/translator/api/projects", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": requestKey.key
        },
        signal: lifecycleController.signal,
        body: JSON.stringify(payload)
      });
      createRequestState = null;
      closeCreateModal();
      await loadProjects();
    } catch (error) {
      createError = error.message || "Failed to create project.";
    } finally {
      isCreatingProject = false;
    }
  }

  function requestDelete(project) {
    pendingDelete = project;
    projectError = "";
  }

  async function deleteProject(projectId) {
    if (!projectId || deleteBusy) return;
    deleteBusy = true;
    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}`, {
        method: "DELETE",
        signal: lifecycleController.signal,
        headers: { "Idempotency-Key": createIdempotencyKey("translator.project.delete") }
      });
      pendingDelete = null;
      await loadProjects();
    } catch (error) {
      projectError = error.message || "Failed to delete project. Please retry.";
    } finally {
      deleteBusy = false;
    }
  }

  onMount(async () => {
    await loadModels();
    await loadProjects();
  });

  onDestroy(() => {
    clearPollTimer();
    lifecycleController.abort();
  });
</script>

<main class="translator-projects-page">
  <section class="hero">
    <div>
      <p class="eyebrow">Language operations</p>
      <h1>Translation Projects</h1>
      <p class="subtitle">
        Translate processed documents and timed subtitles through one reviewable workspace, from source analysis to verified export.
      </p>
    </div>
    <div class="hero-actions">
      <button class="btn btn-primary" on:click={openCreateModal}>New Project</button>
    </div>
  </section>

  {#if !isLoadingProjects}
    <section class="project-summary" role="list" aria-label="Translation project summary">
      <div role="listitem"><span>Projects</span><strong>{projects.length}</strong><small>total workspaces</small></div>
      <div role="listitem"><span>Processing</span><strong>{activeProjectCount}</strong><small>active now</small></div>
      <div role="listitem"><span>Ready</span><strong>{readyProjectCount}</strong><small>available for review</small></div>
      <div role="listitem"><span>Attention</span><strong>{attentionProjectCount}</strong><small>need a decision</small></div>
    </section>
  {/if}

  {#if projectError}
    <div class="alert error" role="alert">
      <span>{projectError}</span>
      <button class="btn btn-ghost btn-compact" type="button" on:click={loadProjects}>Retry</button>
      <button class="btn btn-ghost btn-compact" type="button" on:click={() => (projectError = "")}>Dismiss</button>
    </div>
  {/if}

  {#if modelLoadError || (!isLoadingModels && !modelOptions.length)}
    <div class="alert warning" role="status">
      <span>{modelLoadError || modelSetupMessage(modelOptions, isLoadingModels)}</span>
      <a class="btn btn-ghost btn-compact" href={modelSettingsHref()}>Open Model Settings</a>
      <button class="btn btn-ghost btn-compact" type="button" on:click={loadModels}>Retry</button>
    </div>
  {/if}

  {#if isLoadingProjects}
    <div class="state-card">Loading projects...</div>
  {:else if !projects.length}
    <div class="state-card">
      <h2>No projects yet</h2>
      <p>Start with a processed document or subtitle project to launch the first translation workspace.</p>
      <button class="btn btn-primary" on:click={openCreateModal}>Create First Project</button>
    </div>
  {:else}
    <div class="project-list">
      {#each projects as project (project.id)}
        <ProjectCard
          {project}
          {modelOptions}
          onOpen={() =>
            context
              ? context.navigate(`/addons/translator/workspace/${project.id}`)
              : (window.location.href = `/addons/translator/workspace/${project.id}`)}
          onDelete={() => requestDelete(project)}
        />
      {/each}
    </div>
  {/if}
</main>

<ProjectCreateDialog
  open={isNewModalOpen}
  {form}
  {modelOptions}
  {modelOptionsByProvider}
  {isLoadingModels}
  {documents}
  {documentsLoadError}
  {isLoadingDocuments}
  {subtitleProjects}
  {subtitleProjectsLoadError}
  {isLoadingSubtitleProjects}
  {researchProviderRows}
  {researchProvidersLoadError}
  {researchToolOptions}
  {researchToolsLoadError}
  {isLoadingResearchProviders}
  {isCreatingProject}
  {canCreateProject}
  {createError}
  settingsHref={modelSettingsHref()}
  onClose={closeCreateModal}
  onSubmit={handleCreateProject}
  onFieldChange={updateCreateFormField}
  onToggleResearchProvider={toggleResearchProvider}
  onToggleResearchTool={toggleResearchTool}
  onReasoningError={(message) => (createError = message)}
/>

<ConfirmationDialog
  open={Boolean(pendingDelete)}
  title="Delete translation project?"
  description={`This permanently removes “${pendingDelete?.name || "this project"}” and its translation history.`}
  confirmLabel="Delete project"
  busy={deleteBusy}
  onclose={() => !deleteBusy && (pendingDelete = null)}
  onconfirm={() => deleteProject(pendingDelete?.id)}
/>

<style>
  .translator-projects-page {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: clamp(1rem, 3vw, 2rem);
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    background: var(--color-paper);
  }

  .translator-projects-page > :global(*) {
    width: min(1180px, 100%);
    margin-inline: auto;
  }

  .hero {
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 0 0 clamp(1.25rem, 3vw, 2rem);
    display: flex;
    justify-content: space-between;
    gap: 2rem;
    align-items: end;
  }

  .hero-actions {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    flex-shrink: 0;
  }

  .eyebrow {
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.66rem;
    font-weight: 800;
    color: var(--color-clay);
    margin: 0 0 0.65rem;
  }

  h1 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(2.05rem, 5vw, 3.75rem);
    font-weight: 450;
    letter-spacing: -0.035em;
    line-height: 0.98;
  }

  .subtitle {
    margin: 1rem 0 0;
    color: var(--color-ink-muted);
    max-width: 42rem;
    font-size: clamp(0.88rem, 1.4vw, 1rem);
    line-height: 1.65;
  }

  .project-summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-block: 1px solid var(--color-rule);
    background: color-mix(in srgb, var(--color-surface) 58%, transparent);
  }

  .project-summary > div {
    min-width: 0;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.15rem 0.65rem;
    padding: 0.8rem 1rem;
    border-right: 1px solid var(--color-rule);
  }

  .project-summary > div:last-child { border-right: 0; }
  .project-summary span { grid-column: 1 / -1; color: var(--color-clay); font-size: 0.62rem; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; }
  .project-summary strong { font-family: var(--font-editorial); font-size: 1.65rem; font-weight: 450; line-height: 1; }
  .project-summary small { align-self: end; color: var(--color-ink-muted); font-size: 0.7rem; line-height: 1.25; }

  .project-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .state-card {
    border: 1px dashed var(--color-rule);
    background: var(--color-surface);
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
    align-items: start;
  }

  .btn {
    border: 1px solid transparent;
    min-height: 2.5rem;
    padding: 0.62rem 0.9rem;
    cursor: pointer;
    font-size: 0.78rem;
    font-weight: 650;
    letter-spacing: 0.025em;
  }

  .btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .btn-primary {
    background: var(--color-umber);
    border-color: var(--color-umber);
    color: var(--color-surface);
  }

  :global(.btn-danger) {
    background: var(--color-danger-muted);
    border-color: color-mix(in srgb, var(--color-danger) 35%, var(--color-rule));
    color: var(--color-danger);
  }

  .btn-compact {
    padding-block: 0.4rem;
    font-size: 0.82rem;
  }

  .btn-ghost {
    background: transparent;
    border-color: var(--color-rule);
    color: var(--color-ink-muted);
  }

  .alert {
    border: 1px solid transparent;
    padding: 0.6rem 0.75rem;
    font-size: 0.9rem;
  }

  .alert.error {
    border-color: color-mix(in srgb, var(--color-danger) 35%, var(--color-rule));
    color: var(--color-danger);
    background: var(--color-danger-muted);
  }

  @media (max-width: 760px) {
    .translator-projects-page {
      padding: 1rem;
    }

    .hero,
    .hero-actions {
      flex-direction: column;
      align-items: start;
    }

    .project-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .project-summary > div:nth-child(2) { border-right: 0; }
    .project-summary > div:nth-child(-n + 2) { border-bottom: 1px solid var(--color-rule); }

  }

  @media (max-width: 460px) {
    .project-summary { grid-template-columns: 1fr; }
    .project-summary > div { border-right: 0; border-bottom: 1px solid var(--color-rule); }
    .project-summary > div:last-child { border-bottom: 0; }
  }
</style>
