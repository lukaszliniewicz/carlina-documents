<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendAssistantGuidanceDashboard,
    FrontendAssistantGuidanceMutationResponse,
    FrontendAssistantGuidancePromptId,
    FrontendAssistantGuidanceUpdate,
  } from "@carlina/sdk";
  import { Badge, Button, Icon, PageHeader } from "@carlina/ui";

  import {
    guidanceCustomizedPromptCount,
    guidanceHasChanges,
    guidancePromptDrafts,
    guidancePromptUsesDefault,
    type GuidancePromptDrafts,
  } from "../../lib/assistant-guidance.ts";

  interface Props {
    api: FrontendApiClient;
  }

  let { api }: Props = $props();

  let guidance = $state<FrontendAssistantGuidanceDashboard | null>(null);
  let organizationEnabled = $state(false);
  let organizationProfile = $state("");
  let promptDrafts = $state<GuidancePromptDrafts>({
    chief_assistant: "",
    junior_assistant_mandate: "",
    direct_assistant_default: "",
  });
  let selectedPromptId = $state<FrontendAssistantGuidancePromptId>(
    "chief_assistant",
  );
  let loading = $state(true);
  let saving = $state(false);
  let error = $state("");
  let notice = $state("");

  const selectedPrompt = $derived(
    guidance?.prompts.find((prompt) => prompt.id === selectedPromptId) ?? null,
  );
  const selectedDraft = $derived(
    selectedPrompt ? (promptDrafts[selectedPrompt.id] ?? "") : "",
  );
  const hasTruncatedContent = $derived(
    Boolean(
      guidance?.organization.truncated ||
        guidance?.prompts.some((prompt) => prompt.truncated),
    ),
  );
  const dirty = $derived(
    guidance
      ? guidanceHasChanges(
          guidance,
          organizationEnabled,
          organizationProfile,
          promptDrafts,
        )
      : false,
  );
  const customizedCount = $derived(
    guidance
      ? guidanceCustomizedPromptCount(guidance, promptDrafts)
      : 0,
  );

  onMount(() => {
    void loadGuidance();
  });

  function applyGuidance(payload: FrontendAssistantGuidanceDashboard): void {
    guidance = payload;
    organizationEnabled = payload.organization.enabled;
    organizationProfile = payload.organization.profile;
    promptDrafts = guidancePromptDrafts(payload);
    if (!payload.prompts.some((prompt) => prompt.id === selectedPromptId)) {
      selectedPromptId = payload.prompts[0]?.id ?? "chief_assistant";
    }
  }

  async function loadGuidance(): Promise<void> {
    loading = true;
    error = "";
    notice = "";
    try {
      const payload =
        await api.request<FrontendAssistantGuidanceDashboard>(
          "/api/frontend/settings/guidance",
        );
      applyGuidance(payload);
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Assistant guidance could not be loaded.";
    } finally {
      loading = false;
    }
  }

  function updateSelectedPrompt(value: string): void {
    if (!selectedPrompt) return;
    promptDrafts[selectedPrompt.id] = value;
    notice = "";
  }

  function restoreSelectedPrompt(): void {
    if (!selectedPrompt) return;
    promptDrafts[selectedPrompt.id] = selectedPrompt.default_value;
    notice = `${selectedPrompt.name} will use the house default after saving.`;
  }

  function discardChanges(): void {
    if (!guidance) return;
    applyGuidance(guidance);
    error = "";
    notice = "Unsaved guidance edits were discarded.";
  }

  async function saveGuidance(): Promise<void> {
    if (!guidance || !dirty || hasTruncatedContent) return;
    saving = true;
    error = "";
    notice = "";
    const prompts = Object.fromEntries(
      guidance.prompts.map((prompt) => [
        prompt.id,
        promptDrafts[prompt.id] ?? "",
      ]),
    ) as Record<FrontendAssistantGuidancePromptId, string>;
    const update: FrontendAssistantGuidanceUpdate = {
      organization: {
        enabled: organizationEnabled,
        profile: organizationProfile,
      },
      prompts,
    };
    try {
      const response =
        await api.request<FrontendAssistantGuidanceMutationResponse>(
          guidance.actions.update_url,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(update),
          },
        );
      applyGuidance(response.guidance);
      notice = response.message || "Assistant guidance updated.";
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Assistant guidance was not saved.";
    } finally {
      saving = false;
    }
  }
</script>

<div class="guidance-page">
  <PageHeader
    eyebrow="Settings / Assistant guidance"
    title="Voice, mandate, context"
    description="Shape the shared institutional context and the three core roles that guide assistant work. The editor shows effective prose, while unchanged roles continue to inherit maintained house defaults."
  >
    {#snippet actions()}
      <Button
        variant="outline"
        onclick={discardChanges}
        disabled={!dirty || saving}
      >
        Discard edits
      </Button>
      <Button
        onclick={() => void saveGuidance()}
        disabled={!dirty || saving || hasTruncatedContent}
      >
        <Icon name="check" size={15} />
        {saving ? "Saving…" : "Save guidance"}
      </Button>
    {/snippet}
  </PageHeader>

  {#if error}
    <p class="desk-message desk-message--error" role="alert">{error}</p>
  {:else if notice}
    <p class="desk-message" role="status">
      <Icon name="check" size={14} />
      {notice}
    </p>
  {/if}

  {#if loading}
    <section
      class="loading-state"
      aria-label="Loading assistant guidance"
      aria-busy="true"
    >
      <span></span><span></span><span></span>
    </section>
  {:else if guidance}
    {#if hasTruncatedContent}
      <section class="limit-warning" role="alert">
        <Icon name="shield" size={18} />
        <div>
          <strong>Legacy guidance exceeds the safe editor limit.</strong>
          <p>
            Saving is disabled here to prevent accidental data loss. Review the
            oversized value through Advanced settings before returning to this
            desk.
          </p>
        </div>
      </section>
    {/if}

    <section class="summary-strip" aria-label="Assistant guidance summary">
      <article>
        <p>Organization context</p>
        <strong>{organizationEnabled ? "Active" : "Quiet"}</strong>
        <span>
          {organizationProfile.trim().length.toLocaleString()} /
          {guidance.limits.organization_profile_chars.toLocaleString()} characters
        </span>
      </article>
      <article>
        <p>Core roles</p>
        <strong>{guidance.summary.total_prompts}</strong>
        <span>chief, delegated, and direct</span>
      </article>
      <article>
        <p>Custom editions</p>
        <strong>{customizedCount}</strong>
        <span>
          {customizedCount === 1 ? "role departs" : "roles depart"} from house copy
        </span>
      </article>
    </section>

    <div class="guidance-layout">
      <section class="organization-panel">
        <header>
          <p>Institutional margin</p>
          <h2>Organization profile</h2>
          <span>
            Shared context placed ahead of every assistant when enabled.
          </span>
        </header>

        <label class="switch-row" for="organization-guidance-enabled">
          <input
            id="organization-guidance-enabled"
            type="checkbox"
            bind:checked={organizationEnabled}
          />
          <span class="switch-mark" aria-hidden="true"></span>
          <span>
            <strong>Include organization context</strong>
            <small>
              Applies this profile globally without changing personal profile
              notes.
            </small>
          </span>
        </label>

        <div class="field">
          <div class="field-heading">
            <label for="organization-profile">Shared profile</label>
            <span>
              {organizationProfile.length.toLocaleString()} /
              {guidance.limits.organization_profile_chars.toLocaleString()}
            </span>
          </div>
          <textarea
            id="organization-profile"
            rows="13"
            maxlength={guidance.limits.organization_profile_chars}
            bind:value={organizationProfile}
            placeholder="Describe the organization, its audience, tone, priorities, and non-negotiable principles…"
          ></textarea>
          <p>
            Keep this durable and broadly useful. Project instructions and
            temporary campaigns belong in their own workspaces.
          </p>
        </div>
      </section>

      <section class="prompt-panel">
        <header class="prompt-heading">
          <div>
            <p>Core prompt folio</p>
            <h2>Assistant roles</h2>
          </div>
          <Badge tone={customizedCount ? "warning" : "neutral"}>
            {customizedCount ? `${customizedCount} custom` : "House defaults"}
          </Badge>
        </header>

        <div class="prompt-desk">
          <nav aria-label="Assistant prompt roles">
            {#each guidance.prompts as prompt, index (prompt.id)}
              {@const usesDefault = guidancePromptUsesDefault(
                promptDrafts[prompt.id] ?? "",
                prompt.default_value,
              )}
              <button
                type="button"
                class:selected={prompt.id === selectedPromptId}
                aria-pressed={prompt.id === selectedPromptId}
                onclick={() => {
                  selectedPromptId = prompt.id;
                  notice = "";
                }}
              >
                <span class="prompt-index">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span class="prompt-copy">
                  <strong>{prompt.name}</strong>
                  <small>{usesDefault ? "House default" : "Custom edition"}</small>
                </span>
                <span class:customized={!usesDefault} class="prompt-mark"></span>
              </button>
            {/each}
          </nav>

          {#if selectedPrompt}
            <div class="prompt-editor">
              <div class="prompt-editor__intro">
                <div>
                  <p>Selected role</p>
                  <h3>{selectedPrompt.name}</h3>
                  <span>{selectedPrompt.description}</span>
                </div>
                <Badge
                  tone={guidancePromptUsesDefault(
                    selectedDraft,
                    selectedPrompt.default_value,
                  )
                    ? "neutral"
                    : "warning"}
                >
                  {guidancePromptUsesDefault(
                    selectedDraft,
                    selectedPrompt.default_value,
                  )
                    ? "House default"
                    : "Customized"}
                </Badge>
              </div>

              <label for={`guidance-prompt-${selectedPrompt.id}`}>
                System guidance
              </label>
              <textarea
                id={`guidance-prompt-${selectedPrompt.id}`}
                rows="18"
                maxlength={guidance.limits.prompt_chars}
                value={selectedDraft}
                oninput={(event) =>
                  updateSelectedPrompt(event.currentTarget.value)}
                aria-describedby="prompt-editor-note"
              ></textarea>

              <footer>
                <p id="prompt-editor-note">
                  {selectedDraft.length.toLocaleString()} /
                  {guidance.limits.prompt_chars.toLocaleString()} characters.
                  Leaving this blank also restores the maintained house default.
                </p>
                <Button
                  variant="quiet"
                  size="sm"
                  onclick={restoreSelectedPrompt}
                  disabled={guidancePromptUsesDefault(
                    selectedDraft,
                    selectedPrompt.default_value,
                  )}
                >
                  <Icon name="refresh" size={14} />
                  Restore house default
                </Button>
              </footer>
            </div>
          {/if}
        </div>
      </section>
    </div>

    <footer class="save-ledger">
      <div>
        <span class:changed={dirty}></span>
        <p>
          <strong>{dirty ? "Unpublished edits" : "Guidance is current"}</strong>
          <small>
            Changes affect new assistant turns after they are saved.
          </small>
        </p>
      </div>
      <Button
        onclick={() => void saveGuidance()}
        disabled={!dirty || saving || hasTruncatedContent}
      >
        {saving ? "Saving guidance…" : "Publish guidance changes"}
      </Button>
    </footer>
  {:else}
    <section class="unavailable-state">
      <Icon name="shield" size={24} />
      <h2>Guidance desk unavailable</h2>
      <p>
        This administrator-only surface could not be opened with the current
        session.
      </p>
      <Button variant="outline" onclick={() => void loadGuidance()}>
        Try again
      </Button>
    </section>
  {/if}
</div>

<style>
  .guidance-page {
    width: min(100%, 100rem);
    margin: 0 auto;
    padding: clamp(1.5rem, 4vw, 3.5rem);
  }

  .desk-message {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border-bottom: 1px solid var(--color-rule);
    margin: 0;
    padding: 0.85rem 0;
    color: var(--color-olive);
    font-size: 0.8rem;
  }

  .desk-message--error {
    color: var(--color-danger);
  }

  .loading-state {
    display: grid;
    grid-template-columns: 0.8fr 1.2fr 1.2fr;
    gap: 1px;
    margin-top: 2rem;
    background: var(--color-rule);
  }

  .loading-state span {
    min-height: 24rem;
    background:
      linear-gradient(
        100deg,
        transparent 35%,
        color-mix(in srgb, var(--color-clay) 7%, transparent) 50%,
        transparent 65%
      ),
      var(--color-surface);
    background-size: 220% 100%;
    animation: guidance-shimmer 1.6s linear infinite;
  }

  .limit-warning {
    display: flex;
    align-items: flex-start;
    gap: 0.8rem;
    border: 1px solid var(--color-clay);
    margin-top: 1.5rem;
    padding: 1rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
  }

  .limit-warning strong {
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 550;
  }

  .limit-warning p {
    margin: 0.35rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.78rem;
    line-height: 1.55;
  }

  .summary-strip {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border-block: 1px solid var(--color-rule-strong);
    margin-top: 2rem;
  }

  .summary-strip article {
    min-width: 0;
    padding: 1rem 1.2rem;
  }

  .summary-strip article + article {
    border-inline-start: 1px solid var(--color-rule);
  }

  .summary-strip p,
  .organization-panel header p,
  .prompt-heading p,
  .prompt-editor__intro p {
    margin: 0 0 0.35rem;
    color: var(--color-clay);
    font-size: 0.61rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .summary-strip strong {
    display: block;
    font-family: var(--font-editorial);
    font-size: 1.55rem;
    font-weight: 450;
  }

  .summary-strip span {
    display: block;
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
  }

  .guidance-layout {
    display: grid;
    grid-template-columns: minmax(17rem, 0.72fr) minmax(0, 1.8fr);
    border-bottom: 1px solid var(--color-rule-strong);
  }

  .organization-panel {
    border-inline-end: 1px solid var(--color-rule-strong);
    padding: 2rem 1.5rem 2rem 0;
  }

  .organization-panel header,
  .prompt-heading {
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 1rem;
  }

  .organization-panel h2,
  .prompt-heading h2,
  .prompt-editor__intro h3,
  .unavailable-state h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-weight: 450;
    letter-spacing: -0.02em;
  }

  .organization-panel h2,
  .prompt-heading h2 {
    font-size: 1.55rem;
  }

  .organization-panel header span,
  .prompt-editor__intro span {
    display: block;
    margin-top: 0.5rem;
    color: var(--color-ink-muted);
    font-size: 0.77rem;
    line-height: 1.55;
  }

  .switch-row {
    min-width: 0;
    display: grid;
    grid-template-columns: 1.15rem minmax(0, 1fr);
    align-items: start;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding-block: 1.1rem;
    cursor: pointer;
  }

  .switch-row input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .switch-mark {
    width: 1.1rem;
    height: 1.1rem;
    border: 1px solid var(--color-rule-strong);
    margin-top: 0.1rem;
    background: var(--color-paper);
  }

  .switch-row input:checked + .switch-mark {
    border-color: var(--color-olive);
    box-shadow: inset 0 0 0 3px var(--color-paper);
    background: var(--color-olive);
  }

  .switch-row input:focus-visible + .switch-mark {
    outline: 2px solid var(--color-clay);
    outline-offset: 2px;
  }

  .switch-row strong,
  .switch-row small {
    display: block;
    overflow-wrap: anywhere;
  }

  .switch-row strong {
    font-size: 0.8rem;
  }

  .switch-row small {
    margin-top: 0.3rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.45;
  }

  .field {
    margin-top: 1.25rem;
  }

  .field-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.5rem;
  }

  .field-heading label,
  .prompt-editor > label {
    color: var(--color-ink-soft);
    font-size: 0.67rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .field-heading span {
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.61rem;
  }

  textarea {
    width: 100%;
    border: 1px solid var(--color-rule-strong);
    border-radius: 0;
    padding: 0.9rem;
    resize: vertical;
    background: var(--color-paper);
    color: var(--color-ink);
    font: inherit;
    font-size: 0.78rem;
    line-height: 1.65;
  }

  textarea:focus {
    outline: 2px solid
      color-mix(in srgb, var(--color-clay) 42%, transparent);
    outline-offset: 1px;
  }

  .field > p {
    margin: 0.55rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .prompt-panel {
    min-width: 0;
    padding: 2rem 0 2rem 1.5rem;
  }

  .prompt-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 1rem;
  }

  .prompt-desk {
    display: grid;
    grid-template-columns: minmax(12rem, 0.62fr) minmax(0, 1.5fr);
    border-bottom: 1px solid var(--color-rule);
  }

  .prompt-desk nav {
    border-inline-end: 1px solid var(--color-rule);
  }

  .prompt-desk nav button {
    width: 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem 0.8rem;
    cursor: pointer;
    background: transparent;
    color: var(--color-ink-muted);
    text-align: left;
  }

  .prompt-desk nav button:hover,
  .prompt-desk nav button.selected {
    background: var(--color-surface);
    color: var(--color-ink);
  }

  .prompt-desk nav button.selected {
    box-shadow: inset 3px 0 0 var(--color-clay);
  }

  .prompt-index {
    color: var(--color-rule-strong);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
  }

  .prompt-copy {
    min-width: 0;
  }

  .prompt-copy strong,
  .prompt-copy small {
    display: block;
  }

  .prompt-copy strong {
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .prompt-copy small {
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .prompt-mark {
    width: 0.42rem;
    height: 0.42rem;
    border: 1px solid var(--color-rule-strong);
    background: transparent;
  }

  .prompt-mark.customized {
    border-color: var(--color-clay);
    background: var(--color-clay);
  }

  .prompt-editor {
    min-width: 0;
    padding: 1.25rem;
    background: var(--color-surface);
  }

  .prompt-editor__intro {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    min-height: 6.8rem;
    border-bottom: 1px solid var(--color-rule);
    margin-bottom: 1rem;
    padding-bottom: 1rem;
  }

  .prompt-editor__intro > div {
    max-width: 35rem;
  }

  .prompt-editor__intro h3 {
    font-size: 1.25rem;
  }

  .prompt-editor > label {
    display: block;
    margin-bottom: 0.5rem;
  }

  .prompt-editor textarea {
    min-height: 23rem;
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.72rem;
    line-height: 1.65;
  }

  .prompt-editor footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-top: 0.6rem;
  }

  .prompt-editor footer p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.64rem;
    line-height: 1.45;
  }

  .save-ledger {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding-top: 1.25rem;
  }

  .save-ledger > div {
    display: flex;
    align-items: center;
    gap: 0.7rem;
  }

  .save-ledger > div > span {
    width: 0.5rem;
    height: 0.5rem;
    border: 1px solid var(--color-rule-strong);
  }

  .save-ledger > div > span.changed {
    border-color: var(--color-clay);
    background: var(--color-clay);
  }

  .save-ledger p,
  .save-ledger strong,
  .save-ledger small {
    display: block;
    margin: 0;
  }

  .save-ledger strong {
    font-family: var(--font-editorial);
    font-size: 0.9rem;
    font-weight: 550;
  }

  .save-ledger small {
    margin-top: 0.16rem;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
  }

  .unavailable-state {
    max-width: 34rem;
    border-block: 1px solid var(--color-rule-strong);
    margin: 3rem auto 0;
    padding: 2.5rem 0;
    text-align: center;
  }

  .unavailable-state h2 {
    margin-top: 0.8rem;
    font-size: 1.6rem;
  }

  .unavailable-state p {
    margin: 0.7rem auto 1.2rem;
    color: var(--color-ink-muted);
    font-size: 0.8rem;
    line-height: 1.6;
  }

  @keyframes guidance-shimmer {
    to {
      background-position: -220% 0;
    }
  }

  @media (max-width: 72rem) {
    .guidance-layout {
      display: block;
    }

    .organization-panel {
      border-inline-end: 0;
      border-bottom: 1px solid var(--color-rule-strong);
      padding-right: 0;
    }

    .prompt-panel {
      padding-left: 0;
    }
  }

  @media (max-width: 48rem) {
    .guidance-page {
      padding-inline: 1rem;
    }

    .summary-strip {
      grid-template-columns: 1fr;
    }

    .summary-strip article + article {
      border-top: 1px solid var(--color-rule);
      border-inline-start: 0;
    }

    .prompt-desk {
      display: block;
    }

    .prompt-desk nav {
      display: flex;
      overflow-x: auto;
      border-inline-end: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .prompt-desk nav button {
      min-width: 12rem;
      border-inline-end: 1px solid var(--color-rule);
      border-bottom: 0;
    }

    .prompt-desk nav button.selected {
      box-shadow: inset 0 -3px 0 var(--color-clay);
    }

    .prompt-editor {
      padding-inline: 0;
      background: transparent;
    }

    .prompt-editor__intro,
    .prompt-editor footer,
    .save-ledger {
      align-items: stretch;
      flex-direction: column;
    }

    .prompt-editor__intro {
      min-height: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .loading-state span {
      animation: none;
    }
  }
</style>
