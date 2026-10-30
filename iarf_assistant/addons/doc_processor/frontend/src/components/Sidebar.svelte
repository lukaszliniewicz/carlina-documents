<script lang="ts">
  import { createEventDispatcher, onMount } from "svelte";
  import type { DocumentProject, EngineOption, ModelOption, ProcessingSettings } from "../lib/types";
  import PromptSettingsPanel from "./PromptSettingsPanel.svelte";
  import UploadPanel from "./UploadPanel.svelte";

  export let engines: EngineOption[] = [];
  export let models: ModelOption[] = [];
  export let settings: ProcessingSettings;
  export let projects: DocumentProject[] = [];
  export let activeProjectId: string | null = null;
  export let uploadBusy = false;
  export let promptSaving = false;
  export let creatingProject = false;
  export let uploadResetToken = 0;
  let expanded = true;
  onMount(() => { expanded = window.innerWidth >= 900; });

  const dispatch = createEventDispatcher<{
    upload: File;
    openPromptStudio: void;
    selectLibrary: void;
    selectProject: string;
    createProject: string;
  }>();
</script>

<aside class="dp-sidebar">
  <details class="dp-source-setup" bind:open={expanded}>
  <summary class="dp-sidebar-header"><strong>Source setup</strong><small>Add a document or change extraction settings</small></summary>
  <header class="dp-sidebar-header">
    <p class="dp-eyebrow">Extraction desk</p>
    <p>Choose the source, engine, models, and review policy.</p>
  </header>

  <UploadPanel
    bind:settings
    {engines}
    {models}
    busy={uploadBusy}
    resetToken={uploadResetToken}
    {projects}
    {activeProjectId}
    creatingProject={creatingProject}
    on:upload={(event) => dispatch("upload", event.detail)}
    on:selectLibrary={() => dispatch("selectLibrary")}
    on:selectProject={(event) => dispatch("selectProject", event.detail)}
    on:createProject={(event) => dispatch("createProject", event.detail)}
  />

  <PromptSettingsPanel saving={promptSaving} on:open={() => dispatch("openPromptStudio")} />
  </details>
</aside>
