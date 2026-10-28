<script lang="ts">
  import type {
    FrontendAutomationDefinition,
    FrontendAutomationStepDefinition,
    FrontendAutomationTemplate,
  } from "@carlina/sdk";
  import { Badge, Button, Icon } from "@carlina/ui";

  import {
    automationListText,
    cloneAutomationDefinition,
    newAutomationDefinition,
    newAutomationStep,
    normalizeAutomationKey,
    parseAutomationList,
    validateAutomationDefinition,
    type AutomationTemplateSaveInput,
  } from "../../lib/automations.ts";
  import WorkflowGraphPanel from "./WorkflowGraphPanel.svelte";

  interface Props {
    template: FrontendAutomationTemplate | null;
    saving?: boolean;
    error?: string;
    onsave: (payload: AutomationTemplateSaveInput) => void;
    oncancel: () => void;
  }

  let {
    template,
    saving = false,
    error = "",
    onsave,
    oncancel,
  }: Props = $props();

  let title = $state("Untitled automation");
  let description = $state("");
  let isActive = $state(true);
  let changeSummary = $state("Initial version");
  let definition = $state<FrontendAutomationDefinition>(
    newAutomationDefinition(),
  );
  let selectedKey = $state<string | null>(null);
  let showValidation = $state(false);
  let initializedTemplateKey = $state<string | null | undefined>(undefined);

  $effect(() => {
    const templateKey = template?.id ?? null;
    if (initializedTemplateKey === templateKey) return;

    title = template?.title ?? "Untitled automation";
    description = template?.description ?? "";
    isActive = template?.is_active ?? true;
    changeSummary = template
      ? "Refined in the visual builder"
      : "Initial version";
    definition = template
      ? cloneAutomationDefinition(template.definition)
      : newAutomationDefinition();
    selectedKey = definition.steps[0]?.key ?? null;
    showValidation = false;
    initializedTemplateKey = templateKey;
  });

  const issues = $derived(validateAutomationDefinition({
    ...definition,
    name: title,
    description,
  }));
  const selectedIndex = $derived(
    definition.steps.findIndex((step) => step.key === selectedKey),
  );
  const selectedStep = $derived(
    selectedIndex >= 0 ? definition.steps[selectedIndex] ?? null : null,
  );

  function setStepField<K extends keyof FrontendAutomationStepDefinition>(
    field: K,
    value: FrontendAutomationStepDefinition[K],
  ): void {
    if (selectedIndex < 0) return;
    definition.steps[selectedIndex]![field] = value;
  }

  function uniqueStepKey(base: string, excludeIndex = -1): string {
    const root = normalizeAutomationKey(base) || "step";
    const existing = new Set(
      definition.steps
        .filter((_, index) => index !== excludeIndex)
        .map((step) => step.key),
    );
    if (!existing.has(root)) return root;
    let suffix = 2;
    while (existing.has(`${root}_${suffix}`)) suffix += 1;
    return `${root}_${suffix}`;
  }

  function changeStepKey(rawValue: string): void {
    if (!selectedStep) return;
    const previous = selectedStep.key;
    const normalized = normalizeAutomationKey(rawValue);
    const next = normalized
      ? uniqueStepKey(normalized, selectedIndex)
      : "";
    selectedStep.key = next;
    for (const step of definition.steps) {
      step.depends_on = step.depends_on.map((dependency) =>
        dependency === previous ? next : dependency,
      );
    }
    selectedKey = next;
  }

  function addStep(): void {
    const next = newAutomationStep(definition.steps.length + 1);
    next.key = uniqueStepKey(next.key);
    if (selectedStep?.key) next.depends_on = [selectedStep.key];
    definition.steps.push(next);
    renumberSteps();
    selectedKey = next.key;
  }

  function removeSelectedStep(): void {
    if (!selectedStep || definition.steps.length <= 1) return;
    const removedKey = selectedStep.key;
    const removedIndex = selectedIndex;
    definition.steps.splice(removedIndex, 1);
    for (const step of definition.steps) {
      step.depends_on = step.depends_on.filter(
        (dependency) => dependency !== removedKey,
      );
    }
    renumberSteps();
    selectedKey =
      definition.steps[Math.min(removedIndex, definition.steps.length - 1)]
        ?.key ?? null;
  }

  function moveSelected(offset: -1 | 1): void {
    if (selectedIndex < 0) return;
    const targetIndex = selectedIndex + offset;
    if (targetIndex < 0 || targetIndex >= definition.steps.length) return;
    const [step] = definition.steps.splice(selectedIndex, 1);
    if (!step) return;
    definition.steps.splice(targetIndex, 0, step);
    renumberSteps();
  }

  function renumberSteps(): void {
    definition.steps.forEach((step, index) => {
      step.sequence = index + 1;
    });
  }

  function toggleDependency(dependencyKey: string, checked: boolean): void {
    if (!selectedStep) return;
    const values = new Set(selectedStep.depends_on);
    if (checked) values.add(dependencyKey);
    else values.delete(dependencyKey);
    selectedStep.depends_on = [...values];
  }

  function submit(): void {
    showValidation = true;
    if (issues.length || saving) return;
    const normalized = cloneAutomationDefinition({
      ...definition,
      name: title.trim(),
      description: description.trim(),
    });
    onsave({
      title: title.trim(),
      description: description.trim(),
      is_active: isActive,
      definition: normalized,
      change_summary:
        changeSummary.trim() ||
        (template ? "Updated in the visual builder" : "Initial version"),
    });
  }
</script>

<article class="template-editor">
  <header class="editor-header">
    <div>
      <button class="back-action" type="button" onclick={oncancel}>
        <Icon name="arrow-left" size={14} />
        Automation library
      </button>
      <p>{template ? `Template / Version ${template.active_version ?? "—"}` : "Template / New"}</p>
      <h1>{template ? "Refine the workflow" : "Compose an automation"}</h1>
      <span>
        Build a dependency-safe sequence without editing JSON. Every saved
        structural change becomes a recoverable version.
      </span>
    </div>
    <div class="editor-actions">
      {#if template}
        <Badge tone={template.scope === "shared" ? "neutral" : "success"}>
          {template.scope}
        </Badge>
      {/if}
      <Button variant="outline" onclick={oncancel} disabled={saving}>
        Cancel
      </Button>
      <Button onclick={submit} disabled={saving}>
        <Icon name="check" size={15} />
        {saving ? "Saving…" : template ? "Save new version" : "Create template"}
      </Button>
    </div>
  </header>

  {#if error}
    <div class="editor-message editor-message--error" role="alert">
      <Icon name="activity" size={16} />
      <span>{error}</span>
    </div>
  {/if}

  {#if showValidation && issues.length}
    <section class="validation-panel" aria-label="Template validation">
      <header>
        <Icon name="activity" size={17} />
        <strong>{issues.length} {issues.length === 1 ? "detail needs" : "details need"} attention</strong>
      </header>
      <ul>
        {#each issues as issue (`${issue.stepKey}:${issue.field}:${issue.message}`)}
          <li>{issue.message}</li>
        {/each}
      </ul>
    </section>
  {/if}

  <section class="identity-panel" aria-label="Automation identity">
    <label class="field field--title">
      <span>Automation name</span>
      <input
        value={title}
        maxlength="255"
        autocomplete="off"
        oninput={(event) => (title = event.currentTarget.value)}
      />
    </label>
    <label class="field field--description">
      <span>Purpose</span>
      <textarea
        rows="2"
        placeholder="What durable outcome should this automation produce?"
        oninput={(event) => (description = event.currentTarget.value)}
      >{description}</textarea>
    </label>
    <label class="active-field">
      <input
        type="checkbox"
        checked={isActive}
        onchange={(event) => (isActive = event.currentTarget.checked)}
      />
      <span>
        <strong>Available to run</strong>
        <small>Inactive templates remain editable but cannot start new runs.</small>
      </span>
    </label>
  </section>

  <div class="editor-workspace">
    <aside class="step-directory" aria-label="Workflow steps">
      <header>
        <div>
          <p>Sequence</p>
          <h2>{definition.steps.length.toString().padStart(2, "0")} steps</h2>
        </div>
        <Button size="sm" variant="outline" onclick={addStep}>
          <Icon name="plus" size={14} />
          Add
        </Button>
      </header>
      <ol>
        {#each definition.steps as step, index (step.key || index)}
          <li>
            <button
              type="button"
              class:selected={step.key === selectedKey}
              aria-pressed={step.key === selectedKey}
              onclick={() => (selectedKey = step.key)}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <span>
                <strong>{step.title || "Untitled step"}</strong>
                <small>
                  {step.type || "task"}
                  {#if step.depends_on.length}
                    · after {step.depends_on.length}
                  {/if}
                </small>
              </span>
              {#if step.depends_on.length}
                <i aria-hidden="true"></i>
              {/if}
            </button>
          </li>
        {/each}
      </ol>
      <footer>
        <Button
          size="sm"
          variant="quiet"
          onclick={() => moveSelected(-1)}
          disabled={selectedIndex <= 0}
        >
          Move up
        </Button>
        <Button
          size="sm"
          variant="quiet"
          onclick={() => moveSelected(1)}
          disabled={selectedIndex < 0 || selectedIndex >= definition.steps.length - 1}
        >
          Move down
        </Button>
      </footer>
    </aside>

    <section class="step-inspector" aria-live="polite">
      {#if selectedStep}
        <header>
          <div>
            <p>Step {String(selectedIndex + 1).padStart(2, "0")}</p>
            <h2>{selectedStep.title || "Untitled step"}</h2>
          </div>
          <Button
            size="sm"
            variant="danger"
            onclick={removeSelectedStep}
            disabled={definition.steps.length <= 1}
          >
            Remove step
          </Button>
        </header>

        <div class="step-form">
          <label class="field">
            <span>Step title</span>
            <input
              value={selectedStep.title}
              maxlength="255"
              autocomplete="off"
              oninput={(event) => setStepField("title", event.currentTarget.value)}
            />
          </label>
          <label class="field">
            <span>Stable key</span>
            <input
              value={selectedStep.key}
              maxlength="128"
              spellcheck="false"
              autocomplete="off"
              oninput={(event) => changeStepKey(event.currentTarget.value)}
            />
            <small>Used by dependencies and retained across run versions.</small>
          </label>
          <label class="field">
            <span>Step type</span>
            <input
              list="automation-step-types"
              value={selectedStep.type}
              autocomplete="off"
              oninput={(event) => setStepField("type", event.currentTarget.value)}
            />
          </label>
          <datalist id="automation-step-types">
            <option value="task"></option>
            <option value="research"></option>
            <option value="write"></option>
            <option value="transform"></option>
            <option value="review"></option>
            <option value="approval"></option>
            <option value="publish"></option>
          </datalist>
          <label class="field">
            <span>Model role</span>
            <input
              value={selectedStep.assigned_model_role ?? ""}
              placeholder="fast, medium, powerful, or an agent profile id"
              autocomplete="off"
              oninput={(event) =>
                setStepField(
                  "assigned_model_role",
                  event.currentTarget.value.trim() || null,
                )}
            />
          </label>
          <label class="field field--wide">
            <span>Instructions</span>
            <textarea
              rows="5"
              placeholder="Describe the expected work and its completion boundary."
              oninput={(event) =>
                setStepField("instructions", event.currentTarget.value)}
            >{selectedStep.instructions ?? ""}</textarea>
          </label>

          <fieldset class="dependencies field--wide">
            <legend>Runs after</legend>
            {#if definition.steps.length === 1}
              <p>This is the only step, so it begins immediately.</p>
            {:else}
              <div>
                {#each definition.steps as candidate (candidate.key)}
                  {#if candidate.key !== selectedStep.key}
                    <label>
                      <input
                        type="checkbox"
                        checked={selectedStep.depends_on.includes(candidate.key)}
                        onchange={(event) =>
                          toggleDependency(
                            candidate.key,
                            event.currentTarget.checked,
                          )}
                      />
                      <span>
                        <strong>{candidate.title || candidate.key}</strong>
                        <small>{candidate.key}</small>
                      </span>
                    </label>
                  {/if}
                {/each}
              </div>
            {/if}
          </fieldset>

          <details class="bindings field--wide">
            <summary>
              <span>
                <strong>Execution bindings</strong>
                <small>Optional capabilities, skills, tools, and providers.</small>
              </span>
              <Icon name="chevron-right" size={15} />
            </summary>
            <div>
              <label class="field">
                <span>Capabilities</span>
                <input
                  value={automationListText(selectedStep.capability_ids)}
                  placeholder="research.search, documents.compose"
                  oninput={(event) =>
                    setStepField(
                      "capability_ids",
                      parseAutomationList(event.currentTarget.value),
                    )}
                />
              </label>
              <label class="field">
                <span>Skills</span>
                <input
                  value={automationListText(selectedStep.skill_ids)}
                  placeholder="web_search.research"
                  oninput={(event) =>
                    setStepField(
                      "skill_ids",
                      parseAutomationList(event.currentTarget.value),
                    )}
                />
              </label>
              <label class="field">
                <span>Tools</span>
                <input
                  value={automationListText(selectedStep.tool_ids)}
                  placeholder="web_search.text"
                  oninput={(event) =>
                    setStepField(
                      "tool_ids",
                      parseAutomationList(event.currentTarget.value),
                    )}
                />
              </label>
              <label class="field">
                <span>Providers</span>
                <input
                  value={automationListText(selectedStep.provider_requirements)}
                  placeholder="google, openai"
                  oninput={(event) =>
                    setStepField(
                      "provider_requirements",
                      parseAutomationList(event.currentTarget.value),
                    )}
                />
              </label>
            </div>
          </details>
        </div>
      {/if}
    </section>

    <WorkflowGraphPanel
      definition={{ ...definition, name: title, description }}
      {selectedKey}
      onselect={(key) => (selectedKey = key)}
      eyebrow="Live preview"
      title="Dependency map"
    />
  </div>

  <footer class="save-panel">
    <label class="field">
      <span>Version note</span>
      <input
        value={changeSummary}
        placeholder="What changed in this version?"
        oninput={(event) => (changeSummary = event.currentTarget.value)}
      />
    </label>
    <div>
      <span>{issues.length ? `${issues.length} validation ${issues.length === 1 ? "issue" : "issues"}` : "Ready to save"}</span>
      <Button onclick={submit} disabled={saving}>
        {saving ? "Saving…" : template ? "Save new version" : "Create template"}
      </Button>
    </div>
  </footer>
</article>

<style>
  .template-editor {
    width: min(100%, 112rem);
    margin: 0 auto;
    padding: clamp(1rem, 2.8vw, 2.6rem);
  }

  .editor-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding-bottom: 1.1rem;
  }

  .editor-header > div:first-child {
    max-width: 52rem;
  }

  .back-action {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 0;
    padding: 0 0 0.85rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.65rem;
  }

  .back-action:hover {
    color: var(--color-clay);
  }

  .editor-header p,
  .editor-header h1,
  .editor-header span {
    margin: 0;
  }

  .editor-header p {
    color: var(--color-clay);
    font-size: 0.61rem;
    font-weight: 760;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .editor-header h1 {
    margin-top: 0.28rem;
    font-family: var(--font-editorial);
    font-size: clamp(2rem, 4vw, 3.7rem);
    font-weight: 460;
    letter-spacing: -0.035em;
  }

  .editor-header > div:first-child > span {
    display: block;
    max-width: 43rem;
    margin-top: 0.55rem;
    color: var(--color-ink-muted);
    font-size: 0.79rem;
    line-height: 1.55;
  }

  .editor-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .editor-message,
  .validation-panel {
    margin-top: 1rem;
    border: 1px solid var(--color-danger);
    background: color-mix(in srgb, var(--color-danger) 7%, var(--color-surface));
    color: var(--color-danger);
  }

  .editor-message {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.75rem 0.85rem;
    font-size: 0.72rem;
  }

  .validation-panel {
    padding: 0.85rem 1rem;
  }

  .validation-panel header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .validation-panel strong {
    font-family: var(--font-editorial);
    font-weight: 560;
  }

  .validation-panel ul {
    margin: 0.55rem 0 0 1.25rem;
    padding: 0;
    font-size: 0.7rem;
    line-height: 1.55;
  }

  .identity-panel {
    display: grid;
    grid-template-columns: minmax(14rem, 0.7fr) minmax(20rem, 1.3fr) minmax(14rem, 0.45fr);
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.25rem 0;
  }

  .field {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: 0.35rem;
  }

  .field > span,
  .dependencies legend {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
    font-weight: 740;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .field input,
  .field textarea {
    width: 100%;
    border: 1px solid var(--color-rule-strong);
    border-radius: 0;
    padding: 0.68rem 0.72rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font: inherit;
    font-size: 0.75rem;
  }

  .field textarea {
    resize: vertical;
    line-height: 1.5;
  }

  .field input:focus,
  .field textarea:focus {
    outline: 2px solid var(--color-focus);
    outline-offset: 1px;
  }

  .field > small {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    line-height: 1.45;
  }

  .active-field {
    display: flex;
    align-items: flex-start;
    gap: 0.65rem;
    border-left: 1px solid var(--color-rule);
    padding: 0.45rem 0 0.45rem 1rem;
  }

  .active-field input {
    margin-top: 0.15rem;
    accent-color: var(--color-lichen);
  }

  .active-field span {
    display: grid;
    gap: 0.22rem;
  }

  .active-field strong {
    font-family: var(--font-editorial);
    font-size: 0.9rem;
    font-weight: 560;
  }

  .active-field small {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    line-height: 1.4;
  }

  .editor-workspace {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(14rem, 0.45fr) minmax(20rem, 0.8fr) minmax(27rem, 1.45fr);
    align-items: start;
    gap: 1rem;
    padding-top: 1rem;
  }

  .step-directory,
  .step-inspector {
    min-width: 0;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .step-directory > header,
  .step-inspector > header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.78rem;
  }

  .step-directory header p,
  .step-directory header h2,
  .step-inspector header p,
  .step-inspector header h2 {
    margin: 0;
  }

  .step-directory header p,
  .step-inspector header p {
    color: var(--color-clay);
    font-size: 0.56rem;
    font-weight: 760;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .step-directory header h2,
  .step-inspector header h2 {
    margin-top: 0.12rem;
    font-family: var(--font-editorial);
    font-size: 1.05rem;
    font-weight: 540;
  }

  .step-directory ol {
    max-height: 40rem;
    margin: 0;
    padding: 0;
    overflow: auto;
    list-style: none;
  }

  .step-directory li + li {
    border-top: 1px solid var(--color-rule);
  }

  .step-directory li button {
    width: 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) 3px;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    padding: 0.78rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .step-directory li button:hover,
  .step-directory li button.selected {
    background: var(--color-surface-muted);
  }

  .step-directory li button.selected {
    box-shadow: inset 3px 0 var(--color-clay);
  }

  .step-directory li button > span:first-child {
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.7rem;
  }

  .step-directory li button > span:nth-child(2) {
    min-width: 0;
    display: grid;
    gap: 0.16rem;
  }

  .step-directory li strong {
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.86rem;
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .step-directory li small {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .step-directory li i {
    width: 3px;
    height: 1.6rem;
    background: var(--color-lichen);
  }

  .step-directory > footer {
    display: flex;
    justify-content: space-between;
    border-top: 1px solid var(--color-rule);
    padding: 0.45rem;
  }

  .step-form {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.85rem;
    padding: 0.9rem;
  }

  .field--wide {
    grid-column: 1 / -1;
  }

  .dependencies {
    margin: 0;
    border: 0;
    border-top: 1px solid var(--color-rule);
    padding: 0.9rem 0 0;
  }

  .dependencies > p {
    margin: 0.45rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .dependencies > div {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.35rem;
    margin-top: 0.5rem;
  }

  .dependencies label {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    border: 1px solid var(--color-rule);
    padding: 0.55rem;
    background: var(--color-paper);
  }

  .dependencies input {
    margin-top: 0.12rem;
    accent-color: var(--color-lichen);
  }

  .dependencies label span {
    min-width: 0;
    display: grid;
    gap: 0.12rem;
  }

  .dependencies label strong {
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.74rem;
    font-weight: 540;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dependencies label small {
    color: var(--color-ink-muted);
    font-size: 0.54rem;
  }

  .bindings {
    border-top: 1px solid var(--color-rule);
    padding-top: 0.2rem;
  }

  .bindings summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.7rem 0;
    cursor: pointer;
    list-style: none;
  }

  .bindings summary::-webkit-details-marker {
    display: none;
  }

  .bindings summary span {
    display: grid;
    gap: 0.12rem;
  }

  .bindings summary strong {
    font-family: var(--font-editorial);
    font-size: 0.82rem;
    font-weight: 560;
  }

  .bindings summary small {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .bindings > div {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
    padding-bottom: 0.5rem;
  }

  .save-panel {
    display: grid;
    grid-template-columns: minmax(18rem, 1fr) auto;
    align-items: end;
    gap: 1rem;
    border-top: 1px solid var(--color-rule-strong);
    margin-top: 1rem;
    padding-top: 1rem;
  }

  .save-panel > div {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .save-panel > div > span {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
  }

  @media (max-width: 86rem) {
    .editor-workspace {
      grid-template-columns: minmax(13rem, 0.45fr) minmax(20rem, 0.9fr);
    }

    .editor-workspace > :global(.graph-panel) {
      grid-column: 1 / -1;
    }
  }

  @media (max-width: 62rem) {
    .identity-panel {
      grid-template-columns: 1fr 1fr;
    }

    .active-field {
      grid-column: 1 / -1;
      border-left: 0;
      border-top: 1px solid var(--color-rule);
      padding-left: 0;
      padding-top: 0.8rem;
    }

    .editor-workspace {
      grid-template-columns: minmax(12rem, 0.42fr) minmax(0, 1fr);
    }
  }

  @media (max-width: 46rem) {
    .editor-header {
      align-items: flex-start;
      flex-direction: column;
    }

    .editor-actions {
      width: 100%;
      flex-wrap: wrap;
    }

    .identity-panel,
    .editor-workspace,
    .save-panel {
      grid-template-columns: 1fr;
    }

    .editor-workspace > :global(.graph-panel) {
      grid-column: auto;
    }

    .step-form,
    .dependencies > div,
    .bindings > div {
      grid-template-columns: 1fr;
    }

    .field--wide {
      grid-column: auto;
    }

    .save-panel > div {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
