<script lang="ts">
  import { createEventDispatcher } from "svelte";
  import { previewPromptSettings } from "../lib/api";
  import type {
    ProcessingSettings,
    PromptEditorSection,
    PromptField,
    PromptPreviewResponse,
    PromptSettings
  } from "../lib/types";
  import BaseModal from "./BaseModal.svelte";

  export let open = false;
  export let sections: PromptEditorSection[] = [];
  export let settings: ProcessingSettings;
  export let saving = false;

  const dispatch = createEventDispatcher<{ close: void; save: PromptSettings }>();

  let draft: PromptSettings = {};
  let preview: PromptPreviewResponse | null = null;
  let previewBusy = false;
  let previewError = "";
  let assumePreviousContext = false;
  let lastSectionSeed = "";

  function autoResize(node: HTMLTextAreaElement, _value: string) {
    const resize = () => {
      node.style.height = "0px";
      node.style.height = `${node.scrollHeight}px`;
    };

    resize();
    node.addEventListener("input", resize);

    return {
      update(_nextValue: string) {
        resize();
      },
      destroy() {
        node.removeEventListener("input", resize);
      }
    };
  }

  function createDraftFromSections(useDefaults = false): PromptSettings {
    const next: PromptSettings = {};
    sections.forEach((section) => {
      section.fields.forEach((field) => {
        next[field.id] = useDefaults ? field.default_text : field.resolved_text;
      });
    });
    return next;
  }

  function updateField(fieldId: string, value: string) {
    draft = { ...draft, [fieldId]: value };
  }

  function resetField(field: PromptField) {
    updateField(field.id, field.default_text);
  }

  function resetAll() {
    draft = createDraftFromSections(true);
  }

  function fieldState(field: PromptField): string {
    const currentValue = (draft[field.id] ?? field.resolved_text ?? "").trim();
    const defaultValue = (field.default_text || "").trim();
    const savedOverride = (field.override_text || "").trim();
    if (currentValue === defaultValue) {
      return "Default";
    }
    if (savedOverride && currentValue === savedOverride) {
      return "Saved Override";
    }
    return "Modified";
  }

  function close() {
    dispatch("close");
  }

  function save() {
    dispatch("save", draft);
  }

  async function refreshPreview() {
    previewBusy = true;
    previewError = "";
    try {
      preview = await previewPromptSettings(draft, settings, assumePreviousContext);
    } catch (error) {
      previewError = error instanceof Error ? error.message : "Failed to generate prompt preview.";
    } finally {
      previewBusy = false;
    }
  }

  $: sectionSeed = JSON.stringify(
    sections.map((section) => section.fields.map((field) => [field.id, field.resolved_text]))
  );

  $: if (open && sectionSeed !== lastSectionSeed) {
    draft = createDraftFromSections(false);
    assumePreviousContext = Boolean(settings?.append_previous);
    preview = null;
    previewError = "";
    lastSectionSeed = sectionSeed;
    if (sections.length > 0) {
      void refreshPreview();
    }
  }

  $: if (!open) {
    lastSectionSeed = "";
    preview = null;
    previewError = "";
  }
</script>

<BaseModal
  {open}
  title="Prompts"
  size="full"
  bodyClass="dp-prompt-studio-body"
  on:close={close}
>
  <div class="dp-prompt-studio">
    <aside class="dp-prompt-preview-pane">
      <div class="dp-prompt-preview">
        <div class="dp-prompt-card">
          <div class="dp-prompt-card-header">
            <div>
              <h3>Resolved Preview</h3>
              <p>Preview uses the current processing settings from the sidebar and the draft prompt edits in this modal.</p>
            </div>
            <div class="dp-prompt-preview-actions">
              <label class="dp-inline-check">
                <input
                  type="checkbox"
                  checked={assumePreviousContext}
                  on:change={(event) => {
                    assumePreviousContext = event.currentTarget.checked;
                    void refreshPreview();
                  }}
                />
                Assume previous-page context
              </label>
              <button class="button" type="button" disabled={previewBusy} on:click={refreshPreview}>
                {#if previewBusy}<i class="fa-solid fa-spinner fa-spin"></i>{:else}<i class="fa-solid fa-arrows-rotate"></i>{/if}
                Refresh Preview
              </button>
            </div>
          </div>

          {#if previewError}
            <div class="dp-notice error">{previewError}</div>
          {/if}

          {#if preview}
            <div class="dp-prompt-preview-block">
              <h4>PDF Extraction System Prompt</h4>
              <pre>{preview.extraction.system_prompt}</pre>
            </div>

            <div class="dp-prompt-preview-block">
              <h4>PDF Extraction User Prompt</h4>
              {#if preview.extraction.placeholder_context_used}
                <p class="dp-muted">A placeholder previous-page context block is included to preview the with-context branch.</p>
              {/if}
              <pre>{preview.extraction.user_prompt}</pre>
            </div>

            <div class="dp-prompt-preview-block">
              <h4>Active Extraction Fragments</h4>
              <ul class="dp-prompt-fragments">
                {#each preview.extraction.active_fragments as fragment}
                  <li>
                    <strong>{fragment.name}</strong>
                    <span>{fragment.source}</span>
                  </li>
                {/each}
              </ul>
            </div>

            <details class="dp-prompt-preview-block">
              <summary>Metadata Pre-Pass Prompt</summary>
              <pre>{preview.metadata.system_prompt}</pre>
            </details>

            <details class="dp-prompt-preview-block">
              <summary>Boundary Merge Prompt</summary>
              <pre>{preview.boundary.system_prompt}</pre>
            </details>

            <details class="dp-prompt-preview-block">
              <summary>Evaluation Prompt Template</summary>
              <pre>{preview.evaluation.template}</pre>
            </details>

            <details class="dp-prompt-preview-block">
              <summary>TXT Structuring Prompt</summary>
              <pre>{preview.txt_structuring.template}</pre>
            </details>
          {:else if previewBusy}
            <p class="dp-muted">Building preview...</p>
          {/if}
        </div>
      </div>
    </aside>

    <section class="dp-prompt-editor-pane">
      <div class="dp-prompt-editor">
        {#each sections as section}
          <div class="dp-prompt-card">
            <div class="dp-prompt-card-header">
              <div>
                <h3>{section.title}</h3>
                <p>{section.description}</p>
              </div>
            </div>
            <div class="dp-prompt-fields">
              {#each section.fields as field}
                <label class="dp-prompt-field">
                  <div class="dp-prompt-field-header">
                    <div>
                      <span>{field.name}</span>
                      <small>{field.description}</small>
                    </div>
                    <div class="dp-prompt-field-actions">
                      <em>{fieldState(field)}</em>
                      <button class="button ghost" type="button" on:click={() => resetField(field)}>Reset</button>
                    </div>
                  </div>
                  <textarea
                    rows="8"
                    spellcheck="false"
                    value={draft[field.id] ?? field.resolved_text}
                    use:autoResize={draft[field.id] ?? field.resolved_text}
                    on:input={(event) => updateField(field.id, event.currentTarget.value)}
                  ></textarea>
                </label>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    </section>
  </div>

  <div slot="footer" class="dp-modal-footer">
    <div>
      <button class="button ghost" type="button" disabled={saving} on:click={resetAll}>Reset All</button>
    </div>
    <div>
      <button class="button ghost" type="button" disabled={saving} on:click={close}>Cancel</button>
      <button class="button" type="button" disabled={saving} on:click={save}>
        {#if saving}<i class="fa-solid fa-spinner fa-spin"></i>{:else}<i class="fa-solid fa-floppy-disk"></i>{/if}
        Save Prompts
      </button>
    </div>
  </div>
</BaseModal>
