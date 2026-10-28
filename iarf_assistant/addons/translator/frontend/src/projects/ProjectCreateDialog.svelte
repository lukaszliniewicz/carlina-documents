<script>
  import { Button, Dialog } from "@carlina/ui";
  import ResearchSourcePicker from "../lib/ResearchSourcePicker.svelte";
  import ReasoningLevelSelect from "../lib/ReasoningLevelSelect.svelte";
  import { MAX_CHUNK_SIZE_CHARS } from "../lib/utils.js";
  import { ANNOTATION_PROFILES } from "../lib/annotation-profile.js";
  import { TRANSLATION_CONTEXT_MODES, TRANSLATION_UNIT_MODES, normalizeTranslationContextMode, normalizeTranslationUnitMode } from "../lib/translation-units.js";
  import {
    DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
    DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
    MAX_TRANSLATION_BATCH_MAX_BLOCKS,
    MAX_TRANSLATION_BATCH_INPUT_TOKENS,
    MIN_TRANSLATION_BATCH_MAX_BLOCKS,
    MIN_TRANSLATION_BATCH_INPUT_TOKENS,
    TRANSLATION_BATCH_INPUT_STEP
  } from "../lib/translation-batch-limits.js";

  export let open = false;
  export let form = {};
  export let modelOptions = [];
  export let modelOptionsByProvider = {};
  export let isLoadingModels = false;
  export let documents = [];
  export let documentsLoadError = "";
  export let isLoadingDocuments = false;
  export let subtitleProjects = [];
  export let subtitleProjectsLoadError = "";
  export let isLoadingSubtitleProjects = false;
  export let researchProviderRows = [];
  export let researchProvidersLoadError = "";
  export let researchToolOptions = [];
  export let researchToolsLoadError = "";
  export let isLoadingResearchProviders = false;
  export let isCreatingProject = false;
  export let canCreateProject = false;
  export let createError = "";
  export let settingsHref = "/settings/models";
  export let onClose = () => {};
  export let onSubmit = () => {};
  export let onFieldChange = () => {};
  export let onToggleResearchProvider = () => {};
  export let onToggleResearchTool = () => {};
  export let onReasoningError = () => {};

  function setField(field, value) {
    onFieldChange(field, value);
  }
</script>

<Dialog
  {open}
  title="Create Translation Project"
  description="Choose a snapshotted source and configured models to start a translation operation."
  onclose={() => !isCreatingProject && onClose()}
  size="lg"
>
  <form id="translator-project-create-form" class="modal-form" on:submit={onSubmit}>
    <label>
      Project Name
      <input value={form.name || ""} required on:input={(event) => setField("name", event.currentTarget.value)} />
    </label>

    <div class="two-col">
      <label>
        Source Language
        <input value={form.source_lang || ""} placeholder="Auto-detect" on:input={(event) => setField("source_lang", event.currentTarget.value)} />
      </label>
      <label>
        Target Language
        <input value={form.target_lang || ""} placeholder="e.g. Japanese" required on:input={(event) => setField("target_lang", event.currentTarget.value)} />
      </label>
    </div>

    <label>
      Source Type
      <select value={form.source_type || "document"} on:change={(event) => setField("source_type", event.currentTarget.value)}>
        <option value="document">Processed Document</option>
        <option value="subtitle_studio">Subtitle Studio Project</option>
      </select>
    </label>

    {#if form.source_type === "subtitle_studio"}
      <label>
        Subtitle Project
        <select
          value={form.subtitle_project_id || ""}
          disabled={isLoadingSubtitleProjects || !subtitleProjects.length}
          required
          on:change={(event) => setField("subtitle_project_id", event.currentTarget.value)}
        >
          {#if isLoadingSubtitleProjects}
            <option value="">Loading subtitle projects...</option>
          {:else if !subtitleProjects.length}
            <option value="">No subtitle projects with cues</option>
          {:else}
            {#each subtitleProjects as subtitleProject}
              <option value={subtitleProject.id}>{subtitleProject.name} ({subtitleProject.cue_count} cues)</option>
            {/each}
          {/if}
        </select>
        <small>Prepare and correct timed cues in the configured subtitle source app first.</small>
      </label>
    {:else}
      <label>
        Source Document
        <select
          value={form.document_id || ""}
          disabled={isLoadingDocuments || !documents.length}
          required
          on:change={(event) => setField("document_id", event.currentTarget.value)}
        >
          {#if isLoadingDocuments}
            <option value="">Loading documents...</option>
          {:else if !documents.length}
            <option value="">No completed documents</option>
          {:else}
            {#each documents as doc}
              <option value={doc.id}>{doc.filename}</option>
            {/each}
          {/if}
        </select>
        <small>Missing a document? Upload and finish processing it in the configured document source app.</small>
      </label>
    {/if}

    {#if form.source_type === "subtitle_studio"}
      <p class="engine-default" role="status">
        Translation engine: <strong>Segment engine</strong>
        <small>Subtitle projects always use the timed segment engine.</small>
      </p>
    {:else}
      <p class="engine-default" role="status">
        Translation engine: <strong>Semantic document blocks</strong>
        <small>Document blocks preserve structural context and support many-to-many alignment.</small>
      </p>
    {/if}

    {#if form.source_type === "subtitle_studio" && subtitleProjectsLoadError}
      <div class="alert error">{subtitleProjectsLoadError}</div>
    {:else if form.source_type === "document" && documentsLoadError}
      <div class="alert error">{documentsLoadError}</div>
    {/if}

    {#if form.source_type === "document"}
      <fieldset class="annotation-profile">
        <legend>Review granularity</legend>
        {#each TRANSLATION_UNIT_MODES as mode}
          <label class="profile-option">
            <input
              type="radio"
              name="project-translation-unit-mode"
              value={mode.value}
              checked={normalizeTranslationUnitMode(form.translation_unit_mode) === mode.value}
              disabled={isCreatingProject}
              on:change={() => setField("translation_unit_mode", mode.value)}
            />
            <span><strong>{mode.label}</strong><small>{mode.description}</small></span>
          </label>
        {/each}
        <small>Sentence mode requires a working Sentence Segmenter. Its source boundaries are saved when this project is created.</small>
      </fieldset>
    {/if}

    <details>
      <summary>Advanced Agent Options</summary>
      <div class="advanced">
        {#if form.source_type === "document"}
          <fieldset class="annotation-profile">
            <legend>Passive translator context</legend>
            {#each TRANSLATION_CONTEXT_MODES as mode}
              <label class="profile-option">
                <input
                  type="radio"
                  name="project-translation-context-mode"
                  value={mode.value}
                  checked={normalizeTranslationContextMode(form.translation_context_mode) === mode.value}
                  disabled={isCreatingProject}
                  on:change={() => setField("translation_context_mode", mode.value)}
                />
                <span><strong>{mode.label}</strong><small>{mode.description}</small></span>
              </label>
            {/each}
            <small>Applies to passive translation. Carlina-managed calls receive a short preceding excerpt.</small>
          </fieldset>
        {/if}
        <label class="checkbox">
          <input type="checkbox" checked={Boolean(form.enable_pre_analysis)} on:change={(event) => setField("enable_pre_analysis", event.currentTarget.checked)} />
          Run preliminary terminology analysis
        </label>
        <p class="hint">This scans the source before translation and seeds glossary terms automatically.</p>

        {#if form.source_type === "document" && form.engine_contract === "document-block-v1"}
          <fieldset class="annotation-profile">
            <legend>Reader annotation profile</legend>
            {#each ANNOTATION_PROFILES as profile}
              <label class="profile-option">
                <input
                  type="radio"
                  name="project-annotation-profile"
                  value={profile.value}
                  checked={(form.annotation_profile || "none") === profile.value}
                  on:change={() => setField("annotation_profile", profile.value)}
                />
                <span><strong>{profile.label}</strong><small>{profile.description}</small></span>
              </label>
            {/each}
          </fieldset>
        {:else}
          <label class="checkbox">
            <input type="checkbox" checked={Boolean(form.enable_translator_notes)} on:change={(event) => setField("enable_translator_notes", event.currentTarget.checked)} />
            Allow restrained translator&rsquo;s notes
          </label>
          <p class="hint">The translator may annotate an unusually culture-bound term when it cannot be handled cleanly in the subtitle itself.</p>
        {/if}

        <label class="checkbox">
          <input
            type="checkbox"
            checked={Boolean(form.enable_research)}
            disabled={
              isLoadingResearchProviders ||
              (form.source_type === "document" && form.engine_contract === "document-block-v1"
                ? !form.enable_pre_analysis || !researchToolOptions.some((option) => option.executable && option.selection)
                : !researchProviderRows.length)
            }
            on:change={(event) => setField("enable_research", event.currentTarget.checked)}
          />
          Enable research during translation
        </label>
        <p class="hint">
          {#if form.source_type === "document" && form.engine_contract === "document-block-v1"}
            Research requires preliminary terminology analysis and at least one exact executable tool selection.
          {:else}
            Research is opt-in and only runs when at least one provider is selected. Provider tools are shown below.
          {/if}
        </p>
        {#if isLoadingResearchProviders}
          <p class="hint" role="status">Loading research providers...</p>
        {:else if form.source_type === "document" && form.engine_contract === "document-block-v1" && researchToolsLoadError}
          <div class="alert error" role="alert">{researchToolsLoadError}</div>
        {:else if form.source_type !== "document" || form.engine_contract !== "document-block-v1"}
          {#if researchProvidersLoadError}
          <div class="alert error" role="alert">{researchProvidersLoadError}</div>
          {:else if !researchProviderRows.length}
            <p class="hint">No research providers are active. Translation can still run without research.</p>
          {:else}
            <fieldset class="provider-list">
              <legend>Research providers</legend>
              {#each researchProviderRows as provider (provider.id)}
                <label class="provider-option">
                  <input
                    type="checkbox"
                    checked={form.research_plugin_ids?.includes(provider.id)}
                    on:change={(event) => onToggleResearchProvider(provider.id, event)}
                  />
                  <span>
                    <strong>{provider.name}</strong>
                    {#if provider.unavailable}<em>Needs activation</em>{/if}
                    {#if provider.description}<small>{provider.description}</small>{/if}
                    {#if provider.tools.length}<small>Tools: {provider.tools.map((tool) => tool.name).join(", ")}</small>{/if}
                  </span>
                </label>
              {/each}
            </fieldset>
          {/if}
        {:else if !researchToolOptions.length}
          <p class="hint">No exact read-only research tools are currently available. Translation can still run without research.</p>
        {:else}
          <ResearchSourcePicker
            options={researchToolOptions}
            disabled={isCreatingProject}
            onToggle={onToggleResearchTool}
          />
        {/if}

        <div class="two-col">
          <label>
            Analyzer Model
            <select value={form.analyzer_model || ""} disabled={isLoadingModels} on:change={(event) => setField("analyzer_model", event.currentTarget.value)}>
              {#each Object.entries(modelOptionsByProvider) as [provider, providerModels]}
                <optgroup label={provider}>
                  {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
                </optgroup>
              {/each}
            </select>
            <ReasoningLevelSelect models={modelOptions} modelId={form.analyzer_model} slot="terminology_analysis" onerror={onReasoningError} />
          </label>
          <label>
            Translator Model
            <select value={form.translator_model || ""} disabled={isLoadingModels} on:change={(event) => setField("translator_model", event.currentTarget.value)}>
              {#each Object.entries(modelOptionsByProvider) as [provider, providerModels]}
                <optgroup label={provider}>
                  {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
                </optgroup>
              {/each}
            </select>
            <ReasoningLevelSelect models={modelOptions} modelId={form.translator_model} slot="translation" onerror={onReasoningError} />
          </label>
        </div>

        <label>
          Researcher Sub-Agent Model
          <select value={form.researcher_model || ""} disabled={isLoadingModels} on:change={(event) => setField("researcher_model", event.currentTarget.value)}>
            {#each Object.entries(modelOptionsByProvider) as [provider, providerModels]}
              <optgroup label={provider}>
                {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
              </optgroup>
            {/each}
          </select>
          <ReasoningLevelSelect models={modelOptions} modelId={form.researcher_model} slot="researcher" onerror={onReasoningError} />
        </label>

        {#if form.source_type === "document" && form.engine_contract === "document-block-v1"}
          <label>
            Translation batch input budget
            <input
              type="number"
              min={MIN_TRANSLATION_BATCH_INPUT_TOKENS}
              max={MAX_TRANSLATION_BATCH_INPUT_TOKENS}
              step={TRANSLATION_BATCH_INPUT_STEP}
              value={form.translation_batch_input_tokens ?? DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS}
              on:input={(event) => setField("translation_batch_input_tokens", event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
            />
            <small>Target maximum total prompt size for each translation call. If required context plus one whole block will not fit, the workflow stops before contacting the model and asks you to raise this value.</small>
          </label>
          <label>
            Maximum semantic blocks per batch
            <input
              type="number"
              min={MIN_TRANSLATION_BATCH_MAX_BLOCKS}
              max={MAX_TRANSLATION_BATCH_MAX_BLOCKS}
              step="1"
              value={form.translation_batch_max_blocks ?? DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS}
              on:input={(event) => setField("translation_batch_max_blocks", event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
            />
            <small>The token budget remains primary. Four blocks is the conservative default established with medium-capability models; raise it for stronger models or mostly short structural blocks.</small>
          </label>
        {:else}
          <label>
            Translation Chunk Size (Characters)
            <input
              type="number"
              min="500"
              max={MAX_CHUNK_SIZE_CHARS}
              value={form.chunk_size ?? 2500}
              on:input={(event) => setField("chunk_size", event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
            />
          </label>
        {/if}
      </div>
    </details>

    {#if isCreatingProject && form.source_type === "document" && form.translation_unit_mode === "aligned_sentences"}
      <p class="hint" role="status">Preparing and verifying the source sentences. Starting the local segmenter can take a little longer on its first run.</p>
    {/if}
    {#if createError}<div class="alert error">{createError}</div>{/if}
    {#if !modelOptions.length}<p class="hint">A configured chat model is required. <a href={settingsHref}>Open Model Settings</a>.</p>{/if}
  </form>
  {#snippet footer()}
    <Button variant="quiet" type="button" disabled={isCreatingProject} onclick={onClose}>Cancel</Button>
    <Button
      type="submit"
      form="translator-project-create-form"
      disabled={!canCreateProject || isCreatingProject}
    >
      {isCreatingProject ? "Creating…" : "Create Project"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .modal-form { display: flex; flex-direction: column; gap: 0.9rem; }
  .two-col { display: grid; gap: 0.8rem; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  label { display: flex; flex-direction: column; gap: 0.35rem; font-size: 0.92rem; color: var(--color-ink-soft); }
  input, select { border: 1px solid var(--color-rule); background: var(--color-surface); color: var(--color-ink); border-radius: 0.5rem; padding: 0.55rem 0.6rem; font-size: 0.95rem; }
  details { border: 1px solid var(--color-rule); background: var(--color-paper-muted); border-radius: 0.6rem; padding: 0.6rem; }
  summary { cursor: pointer; font-weight: 600; }
  .advanced { margin-top: 0.75rem; display: flex; flex-direction: column; gap: 0.75rem; }
  .checkbox { flex-direction: row; align-items: center; }
  .hint, small { margin: 0; color: var(--color-ink-muted); font-size: 0.84rem; }
  .engine-default { display: grid; gap: .2rem; margin: 0; padding: .6rem .7rem; border: 1px solid var(--color-rule); border-radius: .45rem; color: var(--color-ink-soft); background: var(--color-paper-muted); font-size: .84rem; }
  .engine-default strong { color: var(--color-ink); }
  .provider-list { display: grid; gap: 0.55rem; margin: 0; padding: 0.7rem; border: 1px solid var(--color-rule); border-radius: 0.45rem; }
  .provider-list legend { padding: 0 0.25rem; font-weight: 600; }
  .provider-option { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 0.55rem; align-items: start; padding: 0.35rem; }
  .provider-option > span { display: grid; gap: 0.15rem; }
  .provider-option small { font-size: 0.78rem; }
  .provider-option em { width: fit-content; color: var(--color-danger); font-size: 0.75rem; font-style: normal; }
  .annotation-profile { display:grid; gap:.48rem; margin:0; padding:.7rem; border:1px solid var(--color-rule); border-radius:.45rem; }
  .annotation-profile legend { padding:0 .25rem; font-weight:600; }
  .profile-option { display:grid; grid-template-columns:auto minmax(0,1fr); align-items:start; gap:.58rem; padding:.5rem; border:1px solid transparent; border-radius:.4rem; cursor:pointer; }
  .profile-option:hover, .profile-option:focus-within { border-color:var(--color-rule); background:var(--color-surface); }
  .profile-option input { width:1rem; height:1rem; margin:.12rem 0 0; padding:0; accent-color:var(--color-active); }
  .profile-option span { display:grid; gap:.14rem; }
  .profile-option strong { color:var(--color-ink); font-size:.88rem; }
  .profile-option small { line-height:1.42; }
  .alert { border: 1px solid transparent; padding: 0.6rem 0.75rem; font-size: 0.9rem; }
  .alert.error { border-color: color-mix(in srgb, var(--color-danger) 35%, var(--color-rule)); color: var(--color-danger); background: var(--color-danger-muted); }
  @media (max-width: 760px) { .two-col { grid-template-columns: 1fr; } }
</style>
