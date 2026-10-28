<script>
  import { Dialog } from "@carlina/ui";
  import ReasoningLevelSelect from "../lib/ReasoningLevelSelect.svelte";
  import ResearchSourcePicker from "../lib/ResearchSourcePicker.svelte";
  import { ANNOTATION_PROFILES, normalizeAnnotationProfile } from "../lib/annotation-profile.js";
  import { TRANSLATION_CONTEXT_MODES, TRANSLATION_UNIT_MODES, normalizeTranslationContextMode, normalizeTranslationUnitMode } from "../lib/translation-units.js";
  import {
    DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
    DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
    MAX_TRANSLATION_BATCH_MAX_BLOCKS,
    MAX_TRANSLATION_BATCH_INPUT_TOKENS,
    MIN_TRANSLATION_BATCH_MAX_BLOCKS,
    MIN_TRANSLATION_BATCH_INPUT_TOKENS,
    TRANSLATION_BATCH_INPUT_STEP,
    isValidTranslationBatchMaxBlocks,
    isValidTranslationBatchInputTokens
  } from "../lib/translation-batch-limits.js";
  import {
    allSelectedResearchToolsExecutable,
    blockResearchToolOptions,
    recommendedResearchToolSelections,
    researchToolSelectionKey,
    selectedResearchToolSelections
  } from "../lib/research-providers.js";

  export let open = false;
  export let models = [];
  export let settingsForm = {};
  export let executionMode = "managed";
  export let settingsBusy = false;
  export let operationActive = false;
  export let exportBusy = false;
  export let settingsError = "";
  export let researchTools = [];
  export let researchToolsLoadError = "";
  export let isLoadingResearchTools = false;
  export let onClose = () => {};
  export let onSubmit = () => {};
  export let onFieldChange = () => {};
  export let onReasoningError = () => {};

  const modelFields = ["translator_model", "analyzer_model", "researcher_model"];
  let reasoningBusySlots = new Set();

  function setField(field, value) {
    onFieldChange(field, value);
  }

  function modelOptionsFor(field, availableModels, form) {
    const options = Array.isArray(availableModels) ? [...availableModels] : [];
    const selected = String(form?.[field] || "").trim();
    if (selected && !options.some((model) => model.id === selected)) {
      options.push({
        id: selected,
        name: `${selected} (currently unavailable)`,
        provider: "Unavailable saved model",
        ready: false,
        unavailable: true
      });
    }
    return options;
  }

  function groupedModelOptions(field, availableModels, form) {
    return modelOptionsFor(field, availableModels, form).reduce((groups, model) => {
      const provider = model.provider || "Other";
      if (!groups[provider]) groups[provider] = [];
      groups[provider].push(model);
      return groups;
    }, {});
  }

  function selectedModel(field) {
    return models.find((model) => model.id === settingsForm?.[field]) || null;
  }

  function selectedModelLabel(field) {
    return selectedModel(field)?.name || String(settingsForm?.[field] || "No model selected");
  }

  function setReasoningBusy(slot, busy) {
    const next = new Set(reasoningBusySlots);
    if (busy) next.add(slot); else next.delete(slot);
    reasoningBusySlots = next;
  }

  function toggleResearchTool(option, event) {
    if (!option?.selection || option.selectionConflict) return;
    const selected = selectedResearchToolSelections(settingsForm.research_tool_selections);
    const key = researchToolSelectionKey(option.selection);
    const next = event.currentTarget.checked
      ? [...selected.filter((item) => researchToolSelectionKey(item) !== key), option.selection]
      : selected.filter((item) => researchToolSelectionKey(item) !== key);
    setField("research_tool_selections", selectedResearchToolSelections(next));
  }

  function setPreliminaryAnalysis(event) {
    // Research depends on the analysis artifact. Keep the invariant visible
    // in the control instead of allowing a doomed PUT request.
    if (settingsForm.enable_research && !event.currentTarget.checked) return;
    setField("enable_pre_analysis", Boolean(event.currentTarget.checked));
  }

  function setResearchEnabled(event) {
    const enabled = Boolean(event.currentTarget.checked);
    if (enabled) {
      setField("enable_pre_analysis", true);
      if (!selectedResearchToolSelections(settingsForm.research_tool_selections).length) {
        const recommended = recommendedResearchToolSelections(researchToolOptions);
        if (recommended.length) setField("research_tool_selections", recommended);
      }
    }
    setField("enable_research", enabled);
  }

  $: selectedTools = selectedResearchToolSelections(settingsForm.research_tool_selections);
  $: researchToolOptions = blockResearchToolOptions(researchTools, selectedTools);
  $: modelGroups = Object.fromEntries(
    modelFields.map((field) => [field, groupedModelOptions(field, models, settingsForm)])
  );
  $: validModelIds = new Set((Array.isArray(models) ? models : []).map((model) => model.id));
  $: passiveMode = String(executionMode || "managed").toLowerCase() === "passive";
  $: modelSelectionValid = passiveMode || (validModelIds.has(settingsForm.translator_model)
    && (!settingsForm.enable_pre_analysis || validModelIds.has(settingsForm.analyzer_model))
    && (!settingsForm.enable_research || validModelIds.has(settingsForm.researcher_model)));
  $: researchSelectionValid = !settingsForm.enable_research
    || (settingsForm.enable_pre_analysis && allSelectedResearchToolsExecutable(researchTools, selectedTools));
  $: batchBudget = Number(settingsForm.translation_batch_input_tokens ?? DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS);
  $: batchBudgetValid = isValidTranslationBatchInputTokens(batchBudget);
  $: batchBlockLimit = Number(settingsForm.translation_batch_max_blocks ?? DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS);
  $: batchBlockLimitValid = isValidTranslationBatchMaxBlocks(batchBlockLimit);
  $: formValid = modelSelectionValid && researchSelectionValid && batchBudgetValid && batchBlockLimitValid;
  $: reasoningSaving = reasoningBusySlots.size > 0;
  $: interactionDisabled = settingsBusy || operationActive || exportBusy || reasoningSaving;
</script>

<Dialog
  {open}
  title="Semantic block settings"
  description="These settings apply to the next block operation only. An operation already in progress keeps its frozen configuration."
  onclose={() => !interactionDisabled && onClose()}
  size="lg"
>
  <form class="modal-form" on:submit={onSubmit}>
    {#if settingsError}<div class="alert error" role="alert">{settingsError}</div>{/if}
    <p class="notice" role="status">
      {passiveMode
        ? "Changes affect the next operation and cannot mutate an in-flight translation or export. The external executor profile is confirmed and frozen separately when the passive workflow starts."
        : "Changes affect the next operation and cannot mutate an in-flight translation or export. Reasoning preferences follow the selected model across your Translator projects."}
    </p>
    {#if reasoningSaving}<p class="hint" role="status">Saving reasoning preference...</p>{/if}

    <div class="settings-sections">
      <details class="settings-section" open>
        <summary>
          <span class="section-title"><strong>Translation</strong><small>{passiveMode ? "External passive executor" : selectedModelLabel("translator_model")}</small></span>
        </summary>
        <div class="section-body">
          <div class="model-control">
            {#if passiveMode}
              <p class="hint" role="status">The confirmed external executor supplies the model and reasoning profile when this passive workflow starts. Carlina model selection is not required here.</p>
            {/if}
            <label>
              Translator model
              <select value={settingsForm.translator_model || ""} disabled={interactionDisabled || passiveMode} on:change={(event) => setField("translator_model", event.currentTarget.value)}>
                {#each Object.entries(modelGroups.translator_model || {}) as [provider, providerModels]}
                  <optgroup label={provider}>
                    {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
                  </optgroup>
                {/each}
              </select>
            </label>
            {#if !passiveMode && selectedModel("translator_model")?.supports_reasoning}
              <ReasoningLevelSelect
                {models}
                modelId={settingsForm.translator_model}
                slot="translation"
                label="Translation reasoning"
                disabled={interactionDisabled}
                onerror={onReasoningError}
                onbusychange={(busy) => setReasoningBusy("translation", busy)}
              />
              {#if selectedModel("translator_model")?.supported_reasoning_levels?.includes("off")}
                <small>Use Off only for direct translation without a hidden reasoning pass.</small>
              {/if}
            {/if}
          </div>

          <fieldset class="choice-profile">
            <legend>Review granularity</legend>
            {#each TRANSLATION_UNIT_MODES as mode}
              <label class="choice-option">
                <input
                  type="radio"
                  name="workspace-translation-unit-mode"
                  value={mode.value}
                  checked={normalizeTranslationUnitMode(settingsForm.translation_unit_mode) === mode.value}
                  disabled={interactionDisabled}
                  on:change={() => setField("translation_unit_mode", mode.value)}
                />
                <span><strong>{mode.label}</strong><small>{mode.description}</small></span>
              </label>
            {/each}
            <small class="choice-footnote">Source sentence boundaries are retained with the project. Older block-mode projects need a new sentence-mode project before switching. Manual edits use the complete formatted block.</small>
          </fieldset>

          {#if executionMode === "passive"}
            <fieldset class="choice-profile">
              <legend>Passive translator context</legend>
              {#each TRANSLATION_CONTEXT_MODES as mode}
                <label class="choice-option">
                  <input
                    type="radio"
                    name="workspace-translation-context-mode"
                    value={mode.value}
                    checked={normalizeTranslationContextMode(settingsForm.translation_context_mode) === mode.value}
                    disabled={interactionDisabled}
                    on:change={() => setField("translation_context_mode", mode.value)}
                  />
                  <span><strong>{mode.label}</strong><small>{mode.description}</small></span>
                </label>
              {/each}
              <small class="choice-footnote">After a new session or a context reset, the translator can request a short preceding excerpt again. Changes apply to the next run.</small>
            </fieldset>
          {/if}

          <div class="two-col">
            <label>
              Translation batch input budget
              <input
                type="number"
                min={MIN_TRANSLATION_BATCH_INPUT_TOKENS}
                max={MAX_TRANSLATION_BATCH_INPUT_TOKENS}
                step={TRANSLATION_BATCH_INPUT_STEP}
                value={settingsForm.translation_batch_input_tokens ?? DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS}
                disabled={interactionDisabled}
                on:input={(event) => setField("translation_batch_input_tokens", Number(event.currentTarget.value))}
              />
              <small>Maximum prompt size per call, including source blocks and shared evidence. 16,000 tokens is the balanced default.</small>
            </label>
            <label>
              Maximum semantic blocks per batch
              <input
                type="number"
                min={MIN_TRANSLATION_BATCH_MAX_BLOCKS}
                max={MAX_TRANSLATION_BATCH_MAX_BLOCKS}
                step="1"
                value={settingsForm.translation_batch_max_blocks ?? DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS}
                disabled={interactionDisabled}
                on:input={(event) => setField("translation_batch_max_blocks", Number(event.currentTarget.value))}
              />
              <small>The token budget remains primary. Four blocks is the conservative medium-model default.</small>
            </label>
          </div>
          <small>If one complete block cannot fit, the workflow stops before contacting the model and asks you to raise the budget.</small>
          {#if !batchBudgetValid}
            <div class="alert error" role="alert">Use a batch budget between 2,048 and 1,000,000 tokens.</div>
          {/if}
          {#if !batchBlockLimitValid}
            <div class="alert error" role="alert">Use a block limit between 1 and 256.</div>
          {/if}

          <fieldset class="choice-profile annotation-profile">
            <legend>Reader annotation profile</legend>
            {#each ANNOTATION_PROFILES as profile}
              <label class="choice-option">
                <input
                  type="radio"
                  name="workspace-annotation-profile"
                  value={profile.value}
                  checked={normalizeAnnotationProfile(settingsForm.annotation_profile, settingsForm.enable_translator_notes) === profile.value}
                  disabled={interactionDisabled}
                  on:change={() => setField("annotation_profile", profile.value)}
                />
                <span><strong>{profile.label}</strong><small>{profile.description}</small></span>
              </label>
            {/each}
          </fieldset>

          <label>
            Custom prompt
            <textarea rows="5" value={settingsForm.custom_prompt || ""} disabled={interactionDisabled} on:input={(event) => setField("custom_prompt", event.currentTarget.value)}></textarea>
            <small>Added to the next block workflow only; archived prompts and in-flight operations remain unchanged.</small>
          </label>
        </div>
      </details>

      <details class="settings-section">
        <summary>
          <span class="section-title"><strong>Preliminary analysis</strong><small>{selectedModelLabel("analyzer_model")}</small></span>
          <span class:enabled={settingsForm.enable_pre_analysis} class="phase-state">{settingsForm.enable_pre_analysis ? "On" : "Off"}</span>
        </summary>
        <div class="section-body">
          <label class="checkbox">
            <input type="checkbox" checked={Boolean(settingsForm.enable_pre_analysis)} disabled={interactionDisabled || settingsForm.enable_research} on:change={setPreliminaryAnalysis} />
            Run preliminary terminology analysis
          </label>
          <small>Build terminology and structural guidance before the next translation operation.</small>
          <div class="model-control">
            <label>
              Analyzer model
              <select value={settingsForm.analyzer_model || ""} disabled={interactionDisabled || !settingsForm.enable_pre_analysis} on:change={(event) => setField("analyzer_model", event.currentTarget.value)}>
                {#each Object.entries(modelGroups.analyzer_model || {}) as [provider, providerModels]}
                  <optgroup label={provider}>
                    {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
                  </optgroup>
                {/each}
              </select>
            </label>
            {#if selectedModel("analyzer_model")?.supports_reasoning}
              <ReasoningLevelSelect
                {models}
                modelId={settingsForm.analyzer_model}
                slot="terminology_analysis"
                label="Analysis reasoning"
                disabled={interactionDisabled || !settingsForm.enable_pre_analysis}
                onerror={onReasoningError}
                onbusychange={(busy) => setReasoningBusy("terminology_analysis", busy)}
              />
            {/if}
          </div>
        </div>
      </details>

      <details class="settings-section">
        <summary>
          <span class="section-title"><strong>Research</strong><small>{selectedModelLabel("researcher_model")}</small></span>
          <span class:enabled={settingsForm.enable_research} class="phase-state">{settingsForm.enable_research ? "On" : "Off"}</span>
        </summary>
        <div class="section-body">
          <label class="checkbox">
            <input type="checkbox" checked={Boolean(settingsForm.enable_research)} disabled={interactionDisabled || isLoadingResearchTools} on:change={setResearchEnabled} />
            Enable preliminary research
          </label>
          <small>Research is opt-in and enables preliminary analysis automatically. Selected sources remain exact and reviewable.</small>
          <div class="model-control">
            <label>
              Researcher model
              <select value={settingsForm.researcher_model || ""} disabled={interactionDisabled || !settingsForm.enable_research} on:change={(event) => setField("researcher_model", event.currentTarget.value)}>
                {#each Object.entries(modelGroups.researcher_model || {}) as [provider, providerModels]}
                  <optgroup label={provider}>
                    {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
                  </optgroup>
                {/each}
              </select>
            </label>
            {#if selectedModel("researcher_model")?.supports_reasoning}
              <ReasoningLevelSelect
                {models}
                modelId={settingsForm.researcher_model}
                slot="researcher"
                label="Research reasoning"
                disabled={interactionDisabled || !settingsForm.enable_research}
                onerror={onReasoningError}
                onbusychange={(busy) => setReasoningBusy("researcher", busy)}
              />
            {/if}
          </div>

          {#if isLoadingResearchTools}
            <p class="hint" role="status">Loading exact research tools...</p>
          {:else if researchToolsLoadError}
            <div class="alert error" role="alert">{researchToolsLoadError}</div>
          {/if}

          <ResearchSourcePicker
            options={researchToolOptions}
            disabled={interactionDisabled}
            onToggle={toggleResearchTool}
          />

          {#if settingsForm.enable_research && !settingsForm.enable_pre_analysis}
            <div class="alert error" role="alert">Research cannot be saved until preliminary terminology analysis is enabled.</div>
          {:else if settingsForm.enable_research && !researchSelectionValid}
            <div class="alert error" role="alert">Research cannot be saved until at least one exact currently executable selection is chosen and unavailable saved selections are unchecked.</div>
          {:else if settingsForm.enable_research && !modelSelectionValid}
            <div class="alert error" role="alert">Select configured, usable chat models for translation, analysis, and research before saving.</div>
          {/if}
        </div>
      </details>
    </div>

    <button class="btn primary" type="submit" disabled={interactionDisabled || !formValid}>
      {settingsBusy ? "Saving..." : "Save settings"}
    </button>
  </form>
</Dialog>

<style>
  .modal-form { display:grid; gap:.78rem; margin-top:.1rem; }
  .settings-sections { display:grid; gap:.62rem; }
  .settings-section { border:1px solid var(--color-rule); background:var(--color-surface); }
  .settings-section summary { display:grid; grid-template-columns:minmax(0,1fr) auto auto; align-items:center; gap:.7rem; padding:.72rem .78rem; cursor:pointer; list-style:none; color:var(--color-ink-soft); }
  .settings-section summary::-webkit-details-marker { display:none; }
  .settings-section summary::after { content:"+"; color:var(--color-ink-muted); font-size:1.05rem; font-weight:400; line-height:1; }
  .settings-section[open] summary::after { content:"−"; }
  .settings-section summary:hover, .settings-section summary:focus-visible { background:var(--color-surface-muted); outline:0; }
  .settings-section summary:focus-visible { box-shadow:inset 0 0 0 1px var(--color-rule-strong); }
  .section-title { display:grid; gap:.12rem; min-width:0; }
  .section-title strong { color:var(--color-ink); font-family:var(--font-editorial,Georgia,serif); font-size:.98rem; font-weight:500; }
  .section-title small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .phase-state { color:var(--color-ink-muted); font-size:.68rem; letter-spacing:.08em; text-transform:uppercase; }
  .phase-state.enabled { color:var(--color-lichen,#56604f); }
  .section-body { display:grid; gap:.76rem; padding:.82rem; border-top:1px solid var(--color-rule); }
  .two-col { display:grid; gap:.8rem; grid-template-columns:repeat(2,minmax(0,1fr)); }
  .model-control { display:grid; align-content:start; gap:.25rem; min-width:0; }
  .choice-profile { display:grid; gap:.42rem; min-width:0; margin:0; padding:.72rem; border:1px solid var(--color-rule); }
  .choice-profile legend { padding:0 .25rem; color:var(--color-ink-soft); font-size:.86rem; font-weight:650; }
  .choice-option { display:grid; grid-template-columns:auto minmax(0,1fr); align-items:start; gap:.58rem; padding:.48rem; border:1px solid transparent; cursor:pointer; }
  .choice-option:hover, .choice-option:focus-within { border-color:var(--color-rule); background:var(--color-surface-muted); }
  .choice-option input { margin:.12rem 0 0; }
  .choice-option span { display:grid; gap:.12rem; }
  .choice-option strong { color:var(--color-ink); font-size:.84rem; }
  .choice-footnote { padding:.1rem .48rem 0; line-height:1.45; }
  label { display:flex; flex-direction:column; gap:.35rem; color:var(--color-ink-soft); font-size:.9rem; }
  label.checkbox { flex-direction:row; align-items:center; gap:.5rem; }
  input, select, textarea { border:1px solid var(--color-rule); border-radius:.5rem; padding:.55rem .6rem; background:var(--color-surface); color:var(--color-ink); font:inherit; }
  input[type="checkbox"] { width:1rem; height:1rem; padding:0; accent-color:var(--color-active); }
  small, .hint { margin:0; color:var(--color-ink-muted); font-size:.78rem; line-height:1.4; }
  .notice { margin:0; padding:.6rem .7rem; border:1px solid color-mix(in srgb,var(--color-active) 28%,var(--color-rule)); border-radius:.45rem; background:var(--color-active-muted); color:var(--color-ink-soft); font-size:.8rem; line-height:1.4; }
  .alert { border:1px solid transparent; padding:.6rem .75rem; font-size:.84rem; line-height:1.4; }
  .alert.error { border-color:color-mix(in srgb,var(--color-danger) 35%,var(--color-rule)); color:var(--color-danger); background:var(--color-danger-muted); }
  .btn { border:1px solid var(--color-rule); border-radius:.48rem; padding:.5rem .76rem; background:var(--color-surface); color:var(--color-ink); cursor:pointer; font:inherit; font-size:.84rem; }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  @media (max-width:760px) { .two-col { grid-template-columns:1fr; } }
</style>
