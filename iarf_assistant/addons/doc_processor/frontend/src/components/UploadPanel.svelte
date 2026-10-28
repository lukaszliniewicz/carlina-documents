<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher, onMount } from "svelte";
  import {
    loadReasoningSettings as fetchReasoningSettings,
    saveReasoningSettings
  } from "../lib/api";
  import {
    ACCEPTED_DOCUMENT_TYPES,
    applyBookQualityProfile,
    normalizeProcessingSettings
  } from "../lib/options";
  import type {
    DocumentProject,
    EngineOption,
    ModelOption,
    ProcessingSettings,
    ReasoningSlots
  } from "../lib/types";
  import ProjectNavigator from "./ProjectNavigator.svelte";

  export let engines: EngineOption[] = [];
  export let models: ModelOption[] = [];
  export let settings: ProcessingSettings;
  export let busy = false;
  export let resetToken = 0;
  export let projects: DocumentProject[] = [];
  export let activeProjectId: string | null = null;
  export let creatingProject = false;

  const dispatch = createEventDispatcher<{
    upload: File;
    selectLibrary: void;
    selectProject: string;
    createProject: string;
  }>();
  let fileInput: HTMLInputElement | undefined;
  let selectedFile: File | null = null;
  let dragOver = false;
  let lastResetToken = resetToken;
  let reasoningSlots: ReasoningSlots = {};
  let formError = "";
  // Keep this as a reactive value instead of hiding the selected-file
  // dependency behind a function call. Svelte's legacy reactive compiler can
  // otherwise miss the file-input update and leave PDF-only controls unmounted.
  $: hasSelectedPdf = Boolean(selectedFile?.name.toLowerCase().endsWith(".pdf"));

  const bookQualityEnforcedFields = new Set<keyof ProcessingSettings>([
    "engine",
    "removehf",
    "ignore_footnotes",
    "simple_footnotes",
    "prompt_cache",
    "metadata_prepass",
    "metadata_pages",
    "metadata_end_pages",
    "metadata_delete_flagged_pages",
    "toc_handling",
    "append_previous",
    "extract_images",
    "correct_images",
    "parallel_pages",
    "skip_cropping"
  ]);

  $: if (resetToken !== lastResetToken) {
    lastResetToken = resetToken;
    selectedFile = null;
    formError = "";
    if (fileInput) {
      fileInput.value = "";
    }
  }

  $: modelsByProvider = models.reduce<Record<string, ModelOption[]>>((acc, model) => {
    const provider = model.provider || "Other";
    if (!acc[provider]) acc[provider] = [];
    acc[provider].push(model);
    return acc;
  }, {});

  function set<K extends keyof ProcessingSettings>(key: K, value: ProcessingSettings[K]) {
    // A profile is an explicit, reproducible set of request fields.  Editing
    // any one of those fields deliberately makes the request Custom instead
    // of silently claiming that Book quality is still in force.
    const processing_profile =
      key === "processing_profile" || settings.processing_profile !== "book_quality" || !bookQualityEnforcedFields.has(key)
        ? settings.processing_profile
        : "custom";
    settings = normalizeProcessingSettings({ ...settings, processing_profile, [key]: value });
  }

  function setProfile(profile: ProcessingSettings["processing_profile"]) {
    settings = profile === "book_quality"
      ? applyBookQualityProfile(settings)
      : normalizeProcessingSettings({ ...settings, processing_profile: "custom" });
  }

  function reasoningLevels(modelId: string): string[] {
    const model = models.find((candidate) => candidate.id === modelId);
    if (!model?.supports_reasoning) return [];
    return model.supported_reasoning_levels?.length ? model.supported_reasoning_levels : [];
  }

  function reasoningValue(slot: string, modelId: string): string {
    const config = reasoningSlots[slot];
    const levels = reasoningLevels(modelId);
    return config?.model_id === modelId && levels.includes(config.level) ? config.level : "default";
  }

  async function loadReasoningSettings() {
    try {
      reasoningSlots = await fetchReasoningSettings();
    } catch (error) {
      console.warn("Unable to load Document Processor reasoning settings.", error);
    }
  }

  async function saveReasoningSetting(slot: string, modelId: string, level: string) {
    const previousSlots = reasoningSlots;
    reasoningSlots = { ...reasoningSlots, [slot]: { model_id: modelId, level } };
    try {
      reasoningSlots = await saveReasoningSettings({
        [slot]: { model_id: modelId, level }
      });
      formError = "";
    } catch (error) {
      reasoningSlots = previousSlots;
      formError = error instanceof Error ? error.message : "Unable to save reasoning settings.";
    }
  }

  onMount(() => {
    void loadReasoningSettings();
  });

  function selectFile(file: File | undefined | null) {
    if (file && !busy) {
      selectedFile = file;
      formError = "";
    }
  }

  function effectiveEngine(): string {
    if (settings.processing_profile === "book_quality") {
      return "llm_vision";
    }
    return settings.engine;
  }

  function parsingModelNeedsVision(): boolean {
    // Custom auto is resolved by the backend and may select a non-vision
    // engine. Book quality and explicit LLM Vision deliberately require it.
    return hasSelectedPdf && effectiveEngine() === "llm_vision";
  }

  function metadataModelNeedsVision(): boolean {
    return hasSelectedPdf
      && settings.metadata_prepass
      && effectiveEngine() === "llm_vision";
  }

  function modelIsVisionReady(modelId: string): boolean {
    return Boolean(models.find((model) => model.id === modelId)?.supports_vision);
  }

  $: profileSummary = settings.processing_profile === "book_quality"
    ? hasSelectedPdf
      ? `Sequential context · metadata pre-pass (${settings.metadata_pages} start + ${settings.metadata_end_pages} end pages) · full footnotes · printed TOC captured as metadata and omitted from body content · optional evaluation defense-in-depth · optional visual boundary correction · image and crop review · prompt cache · running matter removal`
      : "Book quality is the PDF workflow. Select a PDF to configure its extraction, metadata, and review policy; non-PDF sources use their compatible ingestion route."
    : hasSelectedPdf
      ? "Custom/legacy PDF settings are sent exactly as shown below."
      : "Custom PDF settings appear after selecting a PDF; non-PDF sources use their compatible ingestion route.";

  $: qualityTradeoff = settings.processing_profile === "book_quality"
    ? hasSelectedPdf
      ? "Quality-first: this uses a vision-capable extraction model and substantially more model context, review work, time, and cost. Evaluation is optional defense-in-depth; baseline correctness comes from deterministic validation and does not depend on it."
      : "No PDF-specific options are being applied to this upload."
    : hasSelectedPdf
      ? "Custom/legacy: changes below take precedence; reapply Book quality to restore the recommended settings."
      : "Choose a PDF to configure its custom extraction options.";

  function submit() {
    if (selectedFile && !busy) {
      if (!models.length && (parsingModelNeedsVision() || metadataModelNeedsVision())) {
        formError = "No usable models are available. Configure a provider and a vision-capable model in Settings → Models, then refresh this page.";
        return;
      }
      if (parsingModelNeedsVision() && !modelIsVisionReady(settings.parsing_model)) {
        formError = "This PDF run can use LLM vision, but the selected extraction model is not verified vision-capable. Configure/select a usable vision model, then retry.";
        return;
      }
      if (metadataModelNeedsVision() && !modelIsVisionReady(settings.metadata_model)) {
        formError = "This PDF run needs a verified vision-capable metadata model. Configure/select one in Settings → Models, then retry.";
        return;
      }
      formError = "";
      dispatch("upload", selectedFile);
    }
  }
</script>

<form class="dp-upload-form" autocomplete="off" on:submit|preventDefault={submit}>
  <div class="dp-field dp-profile-field">
    <label for="docproc-processing-profile">Processing profile</label>
    <select
      id="docproc-processing-profile"
      value={settings.processing_profile}
      aria-describedby="docproc-processing-profile-summary docproc-processing-profile-tradeoff"
      on:change={(event) => setProfile(event.currentTarget.value === "book_quality" ? "book_quality" : "custom")}
    >
      <option value="book_quality">Book quality (recommended)</option>
      <option value="custom">Custom / legacy</option>
    </select>
    <small id="docproc-processing-profile-summary">{profileSummary}</small>
    <small id="docproc-processing-profile-tradeoff">{qualityTradeoff}</small>
  </div>

  {#if hasSelectedPdf}
  <div class="dp-field dp-engine-field">
    <label for="docproc-engine-select">Extraction Engine</label>
    <select id="docproc-engine-select" value={settings.engine} on:change={(event) => set("engine", event.currentTarget.value)}>
      <option value="auto">Auto (Best Available)</option>
      {#each engines.filter((engine) => engine.id !== "auto") as engine}
        <option value={engine.id}>{engine.name}</option>
      {/each}
    </select>
  </div>
  {/if}

  <ProjectNavigator
    {projects}
    {activeProjectId}
    creating={creatingProject}
    on:selectLibrary={() => dispatch("selectLibrary")}
    on:selectProject={(event) => dispatch("selectProject", event.detail)}
    on:createProject={(event) => dispatch("createProject", event.detail)}
  />

  <button
    class:dragover={dragOver}
    class="dp-dropzone"
    type="button"
    disabled={busy}
    aria-describedby="docproc-upload-status"
    on:click={() => fileInput?.click()}
    on:dragover|preventDefault={() => (dragOver = true)}
    on:dragleave={() => (dragOver = false)}
    on:drop|preventDefault={(event) => {
      dragOver = false;
      if (!busy) selectFile(event.dataTransfer?.files?.[0]);
    }}
  >
    <Icon name="upload" size={28} />
    <span>{selectedFile ? selectedFile.name : "Drag and drop a file or select one"}</span>
    <small>PDF, Office, EPUB, HTML, RTF, email, and text formats</small>
  </button>
  <input bind:this={fileInput} type="file" hidden accept={ACCEPTED_DOCUMENT_TYPES} on:change={(event) => selectFile(event.currentTarget.files?.[0])} />

  {#if selectedFile}
    <div class="dp-selected-file">
      <span title={selectedFile.name}>{selectedFile.name}</span>
      <button class="dp-icon-btn danger" type="button" title="Clear file" aria-label="Clear file" on:click={() => (selectedFile = null)}>
        <Icon name="close" size={15} />
      </button>
    </div>
  {/if}

  {#if formError}
    <p class="dp-inline-error" role="alert" aria-live="assertive"><Icon name="warning" size={15} /> {formError}</p>
  {/if}

  <button class="button dp-primary-action" type="submit" disabled={!selectedFile || busy}>
    {#if busy}<Icon name="refresh" size={16} class="dp-spin" />{:else}<Icon name="brain" size={16} />{/if}
    {busy ? "Uploading..." : "Process Document"}
  </button>
  <p id="docproc-upload-status" class="dp-sr-only" role="status" aria-live="polite">
    {busy ? "Uploading document and starting processing. Please wait." : ""}
  </p>

  {#if hasSelectedPdf}
  <details class="dp-sidebar-section" open>
    <summary><Icon name="settings" size={16} /> Models</summary>
    <div class="dp-option-grid">
      <label>
        <span>Extraction Model</span>
        <select value={settings.parsing_model} on:change={(event) => set("parsing_model", event.currentTarget.value)}>
          {#each Object.entries(modelsByProvider) as [provider, providerModels]}
            <optgroup label={provider}>
              {#each providerModels as model}
                <option value={model.id} disabled={parsingModelNeedsVision() && !model.supports_vision}>{model.name}{model.supports_vision ? "" : " (no vision)"}</option>
              {/each}
            </optgroup>
          {/each}
        </select>
      </label>
      <label>
        <span>Metadata Model</span>
        <select value={settings.metadata_model} on:change={(event) => set("metadata_model", event.currentTarget.value)}>
          {#each Object.entries(modelsByProvider) as [provider, providerModels]}
            <optgroup label={provider}>
              {#each providerModels as model}
                <option value={model.id} disabled={metadataModelNeedsVision() && !model.supports_vision}>{model.name}{model.supports_vision ? "" : " (no vision)"}</option>
              {/each}
            </optgroup>
          {/each}
        </select>
      </label>
      {#if reasoningLevels(settings.parsing_model).length}
        <label>
          <span>Extraction reasoning</span>
          <select
            value={reasoningValue("parsing", settings.parsing_model)}
            on:change={(event) => saveReasoningSetting("parsing", settings.parsing_model, event.currentTarget.value)}
          >
            <option value="default">Model default</option>
            {#each reasoningLevels(settings.parsing_model) as level}
              <option value={level}>{level === "xhigh" ? "Extra high" : level.charAt(0).toUpperCase() + level.slice(1)}</option>
            {/each}
          </select>
        </label>
      {/if}
      {#if reasoningLevels(settings.metadata_model).length}
        <label>
          <span>Metadata reasoning</span>
          <select
            value={reasoningValue("metadata", settings.metadata_model)}
            on:change={(event) => saveReasoningSetting("metadata", settings.metadata_model, event.currentTarget.value)}
          >
            <option value="default">Model default</option>
            {#each reasoningLevels(settings.metadata_model) as level}
              <option value={level}>{level === "xhigh" ? "Extra high" : level.charAt(0).toUpperCase() + level.slice(1)}</option>
            {/each}
          </select>
        </label>
      {/if}
    </div>
  </details>

  {#if effectiveEngine() === "auto" || effectiveEngine() === "llm_vision"}
    <details class="dp-sidebar-section" open>
      <summary><Icon name="file" size={16} /> PDF Cleanup</summary>
      <div class="dp-check-grid">
        <label><input type="checkbox" checked={settings.removehf} on:change={(event) => set("removehf", event.currentTarget.checked)} /> Remove headers and footers</label>
        <label><input type="checkbox" checked={settings.ignore_footnotes} on:change={(event) => set("ignore_footnotes", event.currentTarget.checked)} /> Ignore footnotes</label>
        <label class:disabled={settings.ignore_footnotes}><input type="checkbox" disabled={settings.ignore_footnotes} checked={settings.simple_footnotes} on:change={(event) => set("simple_footnotes", event.currentTarget.checked)} /> Simple footnotes</label>
        <label><input type="checkbox" checked={settings.prompt_cache} on:change={(event) => set("prompt_cache", event.currentTarget.checked)} /> Cache stable prompt context</label>
        <label><input type="checkbox" checked={settings.append_previous} disabled={settings.parallel_pages > 1} on:change={(event) => set("append_previous", event.currentTarget.checked)} /> Include previous page context</label>
        <label><input type="checkbox" checked={settings.evaluate_pdf} on:change={(event) => set("evaluate_pdf", event.currentTarget.checked)} /> Optional two-pass evaluation</label>
        <label><input type="checkbox" checked={settings.llm_boundary_merge} on:change={(event) => set("llm_boundary_merge", event.currentTarget.checked)} /> Optional visual boundary correction</label>
        <label><input type="checkbox" checked={settings.store_debug_artifacts} on:change={(event) => set("store_debug_artifacts", event.currentTarget.checked)} /> Store debug artifacts</label>
      </div>
    </details>

    <details class="dp-sidebar-section" open>
      <summary><Icon name="logs" size={16} /> Metadata</summary>
      <div class="dp-check-grid">
        <label><input type="checkbox" checked={settings.metadata_prepass} on:change={(event) => set("metadata_prepass", event.currentTarget.checked)} /> Metadata pre-pass</label>
      </div>
      <div class="dp-option-row">
        <label>
          <span>Start pages</span>
          <input type="number" min="1" max="100" disabled={!settings.metadata_prepass} value={settings.metadata_pages} on:input={(event) => set("metadata_pages", Number(event.currentTarget.value))} />
        </label>
        <label>
          <span>End pages</span>
          <input type="number" min="0" max="100" disabled={!settings.metadata_prepass} value={settings.metadata_end_pages} on:input={(event) => set("metadata_end_pages", Number(event.currentTarget.value))} />
        </label>
      </div>
      <small>The start and end metadata windows read the whole source document. The output page range below controls extracted content only.</small>
      <div class="dp-check-grid">
        <label class:disabled={!settings.metadata_prepass || !settings.skip_cropping}>
          <input
            type="checkbox"
            checked={settings.metadata_delete_flagged_pages}
            disabled={!settings.metadata_prepass || !settings.skip_cropping}
            on:change={(event) => set("metadata_delete_flagged_pages", event.currentTarget.checked)}
          />
          Delete flagged pages when crop review is skipped
        </label>
      </div>
    </details>

    <details class="dp-sidebar-section" open>
      <summary><Icon name="crop" size={16} /> Images and Cropping</summary>
      <div class="dp-check-grid">
        <label><input type="checkbox" checked={settings.extract_images} on:change={(event) => set("extract_images", event.currentTarget.checked)} /> Extract images</label>
        <label class:disabled={!settings.extract_images}><input type="checkbox" disabled={!settings.extract_images} checked={settings.correct_images} on:change={(event) => set("correct_images", event.currentTarget.checked)} /> Manual bounding-box review</label>
        <label><input type="checkbox" checked={settings.skip_cropping} on:change={(event) => set("skip_cropping", event.currentTarget.checked)} /> Skip crop review</label>
      </div>
    </details>

    <details class="dp-sidebar-section">
      <summary><Icon name="activity" size={16} /> Concurrency</summary>
      <div class="dp-option-grid">
        <label>
          <span>Parallel pages</span>
          <input type="number" min="1" max="10" value={settings.parallel_pages} on:input={(event) => set("parallel_pages", Number(event.currentTarget.value))} />
        </label>
        <label>
          <span>Page range</span>
          <input type="text" value={settings.page_range} placeholder="1-5, 8, 11-13" on:input={(event) => set("page_range", event.currentTarget.value)} />
        </label>
      </div>
    </details>
  {/if}

  {#if effectiveEngine() === "marker"}
    <details class="dp-sidebar-section" open>
      <summary><Icon name="settings" size={16} /> Marker</summary>
      <div class="dp-check-grid">
        <label><input type="checkbox" checked={settings.marker_force_ocr} on:change={(event) => set("marker_force_ocr", event.currentTarget.checked)} /> Force OCR</label>
        <label><input type="checkbox" checked={settings.marker_paginate_output} on:change={(event) => set("marker_paginate_output", event.currentTarget.checked)} /> Paginate output</label>
      </div>
    </details>
  {/if}

  {#if effectiveEngine() === "pymupdf"}
    <details class="dp-sidebar-section" open>
      <summary><Icon name="settings" size={16} /> PyMuPDF</summary>
      <div class="dp-check-grid">
        <label><input type="checkbox" checked={settings.pymupdf_use_ocr} on:change={(event) => set("pymupdf_use_ocr", event.currentTarget.checked)} /> Use standalone OCR</label>
        <label><input type="checkbox" checked={settings.pymupdf_force_ocr} on:change={(event) => set("pymupdf_force_ocr", event.currentTarget.checked)} /> Force OCR</label>
        <label><input type="checkbox" checked={settings.pymupdf_paginate_output} on:change={(event) => set("pymupdf_paginate_output", event.currentTarget.checked)} /> Paginate output</label>
        <label><input type="checkbox" checked={settings.pymupdf_exclude_footers} on:change={(event) => set("pymupdf_exclude_footers", event.currentTarget.checked)} /> Exclude footers</label>
        <label><input type="checkbox" checked={settings.pymupdf_embed_images} on:change={(event) => set("pymupdf_embed_images", event.currentTarget.checked)} /> Embed images</label>
      </div>
    </details>
  {/if}

  {#if effectiveEngine() === "paddleocr"}
    <details class="dp-sidebar-section" open>
      <summary><Icon name="settings" size={16} /> PaddleOCR</summary>
      <div class="dp-option-grid">
        <label>
          <span>Batch size</span>
          <input type="number" min="1" max="50" value={settings.paddle_batch_size} on:input={(event) => set("paddle_batch_size", Number(event.currentTarget.value))} />
        </label>
        <label>
          <span>Layout mode</span>
          <select value={settings.paddle_layout_shape_mode} on:change={(event) => set("paddle_layout_shape_mode", event.currentTarget.value)}>
            <option value="auto">Auto</option>
            <option value="rectangle">Rectangle</option>
            <option value="polygon">Polygon</option>
          </select>
        </label>
      </div>
      <div class="dp-check-grid">
        <label><input type="checkbox" checked={settings.paddle_use_doc_unwarping} on:change={(event) => set("paddle_use_doc_unwarping", event.currentTarget.checked)} /> Document unwarping</label>
        <label><input type="checkbox" checked={settings.paddle_use_doc_orientation_classify} on:change={(event) => set("paddle_use_doc_orientation_classify", event.currentTarget.checked)} /> Orientation classification</label>
        <label><input type="checkbox" checked={settings.paddle_remove_hf} on:change={(event) => set("paddle_remove_hf", event.currentTarget.checked)} /> Remove headers and footers</label>
        <label><input type="checkbox" checked={settings.paddle_preserve_footnotes} on:change={(event) => set("paddle_preserve_footnotes", event.currentTarget.checked)} /> Preserve footnotes</label>
        <label><input type="checkbox" checked={settings.paddle_chart_rec} on:change={(event) => set("paddle_chart_rec", event.currentTarget.checked)} /> Chart recognition</label>
        <label><input type="checkbox" checked={settings.paddle_seal_rec} on:change={(event) => set("paddle_seal_rec", event.currentTarget.checked)} /> Seal recognition</label>
        <label><input type="checkbox" checked={settings.paddle_merge_tables} on:change={(event) => set("paddle_merge_tables", event.currentTarget.checked)} /> Merge tables</label>
        <label><input type="checkbox" checked={settings.paddle_relevel_titles} on:change={(event) => set("paddle_relevel_titles", event.currentTarget.checked)} /> Re-level titles</label>
        <label><input type="checkbox" checked={settings.paddle_concatenate_pages} on:change={(event) => set("paddle_concatenate_pages", event.currentTarget.checked)} /> Concatenate pages</label>
      </div>
    </details>
  {/if}

  {/if}
</form>
