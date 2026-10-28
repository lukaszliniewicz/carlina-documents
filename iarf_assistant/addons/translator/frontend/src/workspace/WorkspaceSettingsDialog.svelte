<script>
  import { Dialog } from "@carlina/ui";
  import ReasoningLevelSelect from "../lib/ReasoningLevelSelect.svelte";
  import { MAX_CHUNK_SIZE_CHARS } from "../lib/utils.js";

  export let open = false;
  export let models = [];
  export let modelsByProvider = {};
  export let settingsForm = {};
  export let settingsBusy = false;
  export let researchProviderRows = [];
  export let researchProvidersLoadError = "";
  export let isLoadingResearchProviders = false;
  export let onClose = () => {};
  export let onSubmit = () => {};
  export let onFieldChange = () => {};
  export let onToggleResearchProvider = () => {};
  export let onReasoningError = () => {};

  function setField(field, value) {
    onFieldChange(field, value);
  }
</script>

<Dialog
  {open}
  title="Project Settings"
  description="Only currently configured, usable chat models are listed."
  onclose={() => !settingsBusy && onClose()}
>
  <form class="modal-form" on:submit={onSubmit}>
    <label>
      Translator Model
      <select value={settingsForm.translator_model || ""} on:change={(event) => setField("translator_model", event.currentTarget.value)}>
        {#each Object.entries(modelsByProvider) as [provider, providerModels]}
          <optgroup label={provider}>
            {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
          </optgroup>
        {/each}
      </select>
      <ReasoningLevelSelect {models} modelId={settingsForm.translator_model} slot="translation" onerror={onReasoningError} />
    </label>
    <label>
      Researcher Sub-Agent Model
      <select value={settingsForm.researcher_model || ""} on:change={(event) => setField("researcher_model", event.currentTarget.value)}>
        {#each Object.entries(modelsByProvider) as [provider, providerModels]}
          <optgroup label={provider}>
            {#each providerModels as model}<option value={model.id}>{model.name}</option>{/each}
          </optgroup>
        {/each}
      </select>
      <ReasoningLevelSelect {models} modelId={settingsForm.researcher_model} slot="researcher" onerror={onReasoningError} />
    </label>
    <label class="checkbox">
      <input type="checkbox" checked={Boolean(settingsForm.enable_research)} disabled={isLoadingResearchProviders || !researchProviderRows.length} on:change={(event) => setField("enable_research", event.currentTarget.checked)} />
      Enable research during translation
    </label>
    <p class="hint">Research is opt-in and is disabled when no provider is selected.</p>
    {#if isLoadingResearchProviders}
      <p class="hint" role="status">Loading research providers...</p>
    {:else if researchProvidersLoadError}
      <div class="alert error" role="alert">{researchProvidersLoadError}</div>
    {:else if !researchProviderRows.length}
      <p class="hint">No research providers are active. Translation can still run without research.</p>
    {:else}
      <fieldset class="provider-list">
        <legend>Research providers</legend>
        {#each researchProviderRows as provider (provider.id)}
          <label class="provider-option">
            <input type="checkbox" checked={settingsForm.research_plugin_ids?.includes(provider.id)} on:change={(event) => onToggleResearchProvider(provider.id, event)} />
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
    <label class="checkbox">
      <input
        type="checkbox"
        checked={Boolean(settingsForm.enable_translator_notes)}
        on:change={(event) => setField("enable_translator_notes", event.currentTarget.checked)}
      />
      Allow restrained translator&rsquo;s notes
    </label>
    <p class="hint">When enabled, the translator may add a brief reader-facing note only when a culturally or philosophically loaded term would otherwise lose material meaning. Notes are omitted from model output and exports when this is off.</p>
    <label>
      Translation Chunk Size
      <input
        type="number"
        min="500"
        max={MAX_CHUNK_SIZE_CHARS}
        value={settingsForm.chunk_size_chars ?? 2500}
        on:input={(event) => setField("chunk_size_chars", event.currentTarget.value === "" ? "" : Number(event.currentTarget.value))}
      />
    </label>
    <label>
      Custom Prompt Override
      <textarea rows="4" value={settingsForm.custom_prompt || ""} on:input={(event) => setField("custom_prompt", event.currentTarget.value)}></textarea>
    </label>
    <button class="btn primary" type="submit" disabled={settingsBusy}>
      {settingsBusy ? "Saving..." : "Save Settings"}
    </button>
  </form>
</Dialog>

<style>
  .modal-form { display: flex; flex-direction: column; gap: 0.72rem; margin-top: 0.1rem; }
  .modal-form label { display: flex; flex-direction: column; gap: 0.35rem; font-size: 0.9rem; color: var(--color-ink-soft); }
  .modal-form label.checkbox { flex-direction: row; align-items: center; gap: 0.5rem; }
  input, select, textarea { border: 1px solid var(--color-rule); background: var(--color-surface); color: var(--color-ink); border-radius: 0.5rem; padding: 0.55rem 0.6rem; font: inherit; }
  .hint, small { margin: 0; font-size: 0.84rem; color: var(--color-ink-muted); }
  .provider-list { display: grid; gap: 0.55rem; margin: 0; padding: 0.7rem; border: 1px solid var(--color-rule); border-radius: 0.45rem; }
  .provider-list legend { padding: 0 0.25rem; font-weight: 600; }
  .provider-option { display: grid !important; grid-template-columns: auto minmax(0, 1fr); gap: 0.55rem !important; align-items: start; padding: 0.35rem; }
  .provider-option > span { display: grid; gap: 0.15rem; }
  .provider-option small { font-size: 0.78rem; }
  .provider-option em { width: fit-content; color: var(--color-danger); font-size: 0.75rem; font-style: normal; }
  .alert { border: 1px solid transparent; padding: 0.6rem 0.75rem; font-size: 0.9rem; }
  .alert.error { border-color: color-mix(in srgb, var(--color-danger) 35%, var(--color-rule)); color: var(--color-danger); background: var(--color-danger-muted); }
  .btn { border: 1px solid var(--color-rule); background: var(--color-surface); color: var(--color-ink); padding: 0.46rem 0.76rem; cursor: pointer; font-size: 0.86rem; border-radius: 0.48rem; }
  .btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .btn.primary { border-color: var(--color-active); background: var(--color-active); color: var(--color-surface); }
</style>
