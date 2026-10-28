<script lang="ts">
  import { untrack } from "svelte";
  import type {
    FrontendAdministrationMutationResponse,
    FrontendApiClient,
    FrontendRetrievalConfiguration,
    FrontendRetrievalPreflightResponse,
    FrontendRetrievalSettings,
  } from "@carlina/sdk";
  import { Badge, Button, Dialog, Icon } from "@carlina/ui";

  import {
    retrievalDraft,
    retrievalDraftIsValid,
    retrievalHasChanges,
    retrievalNeedsPreflight,
    retrievalStructuralChanges,
    streamAdministrationEvents,
  } from "../../../lib/administration.ts";

  interface Props {
    api: FrontendApiClient;
    value: FrontendRetrievalSettings;
    onrefresh: () => Promise<void>;
  }

  let { api, value, onrefresh }: Props = $props();

  let current = $state(untrack(() => value));
  let draft = $state<FrontendRetrievalConfiguration>(
    untrack(() => retrievalDraft(value)),
  );
  let saving = $state(false);
  let preflighting = $state(false);
  let preflightToken = $state("");
  let preflightFingerprint = $state("");
  let rebuildRequired = $state(false);
  let rebuildOpen = $state(false);
  let rebuildReviewOpen = $state(false);
  let confirmation = $state("");
  let rebuilding = $state(false);
  let rebuildProgress = $state(0);
  let rebuildMessage = $state("");
  let error = $state("");
  let notice = $state("");
  let rebuildAbort: AbortController | null = null;

  const dirty = $derived(retrievalHasChanges(current, draft));
  const valid = $derived(retrievalDraftIsValid(current, draft));
  const structuralChanges = $derived(
    retrievalStructuralChanges(current, draft),
  );
  const needsPreflight = $derived(
    retrievalNeedsPreflight(current, draft),
  );
  const selectedModel = $derived(
    current.models.find((model) => model.id === draft.model_id) ?? null,
  );
  const fingerprint = $derived(
    `${draft.model_id}\u0000${draft.dimensions}\u0000${draft.task_type}`,
  );
  const preflightReady = $derived(
    !needsPreflight ||
      Boolean(
        preflightToken &&
          preflightFingerprint === fingerprint,
      ),
  );

  function updateNumber(
    field:
      | "dimensions"
      | "top_k"
      | "chunk_size"
      | "chunk_overlap",
    value: number,
  ): void {
    draft[field] = value;
    error = "";
    notice = "";
  }

  function selectModel(modelId: string): void {
    draft.model_id = modelId;
    const model = current.models.find((item) => item.id === modelId);
    if (
      model?.default_dimensions &&
      (!model.supports_mrl || !Number.isInteger(draft.dimensions))
    ) {
      draft.dimensions = model.default_dimensions;
    }
    preflightToken = "";
    preflightFingerprint = "";
    error = "";
    notice = "";
  }

  function discard(): void {
    draft = retrievalDraft(current);
    preflightToken = "";
    preflightFingerprint = "";
    error = "";
    notice = "Unsaved retrieval edits were discarded.";
  }

  async function runPreflight(): Promise<void> {
    if (!valid) return;
    preflighting = true;
    error = "";
    notice = "";
    try {
      const response =
        await api.request<FrontendRetrievalPreflightResponse>(
          current.actions.preflight_url,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model_id: draft.model_id,
              dimensions: draft.dimensions,
              task_type: draft.task_type,
            }),
          },
        );
      preflightToken = response.preflight_token;
      preflightFingerprint = fingerprint;
      notice =
        `${response.message} Document and query probes returned ` +
        `${response.document_dimensions.toLocaleString()} dimensions.`;
    } catch (reason) {
      preflightToken = "";
      preflightFingerprint = "";
      error =
        reason instanceof Error
          ? reason.message
          : "The embedding preflight did not complete.";
    } finally {
      preflighting = false;
    }
  }

  function reviewSave(): void {
    if (!dirty || !valid || !preflightReady) return;
    if (structuralChanges.length) {
      rebuildReviewOpen = true;
      return;
    }
    void publish(false);
  }

  async function publish(acknowledgeRebuild: boolean): Promise<void> {
    if (!dirty || !valid || !preflightReady) return;
    saving = true;
    error = "";
    notice = "";
    try {
      const response =
        await api.request<FrontendAdministrationMutationResponse>(
          current.actions.update_url,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              ...draft,
              acknowledge_rebuild: acknowledgeRebuild,
              preflight_token: preflightToken,
            }),
          },
        );
      if (response.retrieval) {
        current = response.retrieval;
        draft = retrievalDraft(response.retrieval);
      }
      rebuildRequired = Boolean(response.requires_rebuild);
      rebuildReviewOpen = false;
      preflightToken = "";
      preflightFingerprint = "";
      notice = response.message || "Retrieval settings updated.";
      await onrefresh();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Retrieval settings were not saved.";
    } finally {
      saving = false;
    }
  }

  function openRebuild(): void {
    confirmation = "";
    rebuildOpen = true;
  }

  async function rebuildIndex(): Promise<void> {
    if (confirmation.trim() !== "REBUILD INDEX") return;
    rebuildAbort?.abort();
    rebuildAbort = new AbortController();
    rebuilding = true;
    rebuildProgress = 0;
    rebuildMessage = "Preparing index rebuild…";
    error = "";
    notice = "";
    rebuildOpen = false;
    try {
      let streamError = "";
      await streamAdministrationEvents(
        current.actions.rebuild_url,
        { confirmation: "REBUILD INDEX" },
        (event) => {
          rebuildMessage = event.message;
          if (typeof event.progress === "number") {
            rebuildProgress = Math.max(0, Math.min(100, event.progress));
          }
          if (event.type === "error") streamError = event.message;
        },
        rebuildAbort.signal,
      );
      if (streamError) throw new Error(streamError);
      rebuildProgress = 100;
      rebuildRequired = false;
      notice = rebuildMessage || "Retrieval index rebuilt.";
      await onrefresh();
    } catch (reason) {
      if (rebuildAbort.signal.aborted) return;
      error =
        reason instanceof Error
          ? reason.message
          : "The retrieval index was not rebuilt.";
    } finally {
      rebuilding = false;
    }
  }
</script>

<section class="retrieval-panel" aria-labelledby="retrieval-heading">
  <header class="section-intro">
    <div>
      <p>Semantic infrastructure</p>
      <h2 id="retrieval-heading">Retrieval structure</h2>
    </div>
    <span>
      Test embedding compatibility before structural changes, then rebuild the
      index through a visible, cancellable activity rather than a blind request.
    </span>
  </header>

  {#if error}
    <p class="panel-message panel-message--error" role="alert">{error}</p>
  {:else if notice}
    <p class="panel-message" role="status">
      <Icon name="check" size={14} />
      {notice}
    </p>
  {/if}

  {#if current.configuration_needs_review}
    <section class="configuration-warning" role="alert">
      <Icon name="activity" size={18} />
      <div>
        <strong>One or more stored retrieval values need review.</strong>
        <p>
          Choose an active shared embedding model and bring every numeric value
          within the labelled bounds before publishing.
        </p>
      </div>
    </section>
  {/if}

  <section class="index-reading" aria-label="Retrieval index summary">
    <article>
      <p>Documents</p>
      <strong>{current.index.documents.toLocaleString()}</strong>
      <span>source records</span>
    </article>
    <article>
      <p>Chunks</p>
      <strong>{current.index.chunks.toLocaleString()}</strong>
      <span>searchable passages</span>
    </article>
    <article>
      <p>Embedding model</p>
      <strong>{selectedModel?.name ?? "Unavailable"}</strong>
      <span>{selectedModel?.provider_label ?? draft.model_id}</span>
    </article>
  </section>

  <div class="retrieval-workspace">
    <form
      onsubmit={(event) => {
        event.preventDefault();
        reviewSave();
      }}
    >
      <fieldset>
        <legend>Embedding profile</legend>
        <div class="field-grid">
          <label for="retrieval-model">
            Shared embedding model
            <select
              id="retrieval-model"
              value={draft.model_id}
              onchange={(event) =>
                selectModel(event.currentTarget.value)}
            >
              {#if !current.models.length}
                <option value="">No shared embedding models</option>
              {/if}
              {#each current.models as model (model.id)}
                <option value={model.id}>
                  {model.provider_label} · {model.name}
                </option>
              {/each}
            </select>
          </label>

          <label for="retrieval-dimensions">
            Vector dimensions
            <input
              id="retrieval-dimensions"
              type="number"
              min={current.limits.dimensions.minimum}
              max={current.limits.dimensions.maximum}
              value={draft.dimensions}
              disabled={Boolean(
                selectedModel &&
                  !selectedModel.supports_mrl &&
                  selectedModel.default_dimensions,
              )}
              oninput={(event) =>
                updateNumber(
                  "dimensions",
                  event.currentTarget.valueAsNumber,
                )}
            />
            <small>
              {#if selectedModel && !selectedModel.supports_mrl}
                Fixed by this model at
                {selectedModel.default_dimensions?.toLocaleString() ?? "its provider default"}.
              {:else}
                MRL-capable models may use
                {current.limits.dimensions.minimum.toLocaleString()}–
                {current.limits.dimensions.maximum.toLocaleString()}.
              {/if}
            </small>
          </label>
        </div>

        <div class="task-list" aria-label="Embedding task type">
          {#each current.task_types as task (task.id)}
            <label class:active={draft.task_type === task.id}>
              <input
                type="radio"
                name="retrieval-task"
                value={task.id}
                checked={draft.task_type === task.id}
                onchange={() => {
                  draft.task_type = task.id;
                  preflightToken = "";
                  preflightFingerprint = "";
                }}
              />
              <span>
                <strong>{task.label}</strong>
                <small>{task.description}</small>
              </span>
            </label>
          {/each}
        </div>
      </fieldset>

      <fieldset>
        <legend>Passage and recall balance</legend>
        <div class="number-grid">
          <label for="retrieval-chunk-size">
            Chunk size
            <input
              id="retrieval-chunk-size"
              type="number"
              min={current.limits.chunk_size.minimum}
              max={current.limits.chunk_size.maximum}
              step="100"
              value={draft.chunk_size}
              oninput={(event) =>
                updateNumber(
                  "chunk_size",
                  event.currentTarget.valueAsNumber,
                )}
            />
            <small>Target characters per searchable passage.</small>
          </label>
          <label for="retrieval-overlap">
            Chunk overlap
            <input
              id="retrieval-overlap"
              type="number"
              min={current.limits.chunk_overlap.minimum}
              max={current.limits.chunk_overlap.maximum}
              step="50"
              value={draft.chunk_overlap}
              oninput={(event) =>
                updateNumber(
                  "chunk_overlap",
                  event.currentTarget.valueAsNumber,
                )}
            />
            <small>Must remain smaller than the chunk size.</small>
          </label>
          <label for="retrieval-top-k">
            Results per question
            <input
              id="retrieval-top-k"
              type="number"
              min={current.limits.top_k.minimum}
              max={current.limits.top_k.maximum}
              value={draft.top_k}
              oninput={(event) =>
                updateNumber(
                  "top_k",
                  event.currentTarget.valueAsNumber,
                )}
            />
            <small>Recall breadth without changing index structure.</small>
          </label>
        </div>
      </fieldset>

      {#if structuralChanges.length}
        <section class="change-reading">
          <Icon name="refresh" size={17} />
          <div>
            <strong>Index rebuild required after saving</strong>
            <p>{structuralChanges.join(", ")} will change.</p>
          </div>
        </section>
      {/if}

      <footer>
        <div class="preflight-status">
          {#if needsPreflight}
            <Button
              type="button"
              variant="outline"
              disabled={!valid || preflighting || saving}
              onclick={() => void runPreflight()}
            >
              <Icon name="activity" size={14} />
              {preflighting
                ? "Testing…"
                : preflightReady
                  ? "Test passed"
                  : "Test embedding"}
            </Button>
          {/if}
          <span>
            {needsPreflight && !preflightReady
              ? "A live document/query probe is required."
              : "Configuration is ready for review."}
          </span>
        </div>
        <div class="form-actions">
          <Button
            type="button"
            variant="quiet"
            disabled={!dirty || saving}
            onclick={discard}
          >
            Discard
          </Button>
          <Button
            type="submit"
            disabled={
              !dirty ||
              !valid ||
              !preflightReady ||
              saving
            }
          >
            <Icon name="check" size={14} />
            {saving ? "Saving…" : "Review and save"}
          </Button>
        </div>
      </footer>
    </form>

    <aside class="maintenance-panel">
      <p>Index maintenance</p>
      <h3>Rebuild every vector</h3>
      <p>
        Re-embeds all stored knowledge with the published configuration and
        asks addons with isolated stores to rebuild as well.
      </p>

      {#if rebuilding}
        <div
          class="rebuild-progress"
          role="progressbar"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={rebuildProgress}
        >
          <div>
            <strong>{rebuildProgress}%</strong>
            <span>{rebuildMessage}</span>
          </div>
          <i style={`width: ${rebuildProgress}%`}></i>
        </div>
        <Button
          variant="outline"
          onclick={() => rebuildAbort?.abort()}
        >
          Cancel connection
        </Button>
      {:else}
        {#if rebuildRequired}
          <div class="rebuild-needed">
            <Icon name="activity" size={16} />
            New structure is saved but not yet indexed.
          </div>
        {/if}
        <Button variant="danger" onclick={openRebuild}>
          <Icon name="refresh" size={14} />
          Rebuild index
        </Button>
      {/if}

      <dl>
        <div>
          <dt>Current dimensions</dt>
          <dd>{current.configuration.dimensions.toLocaleString()}</dd>
        </div>
        <div>
          <dt>Current task</dt>
          <dd>{current.configuration.task_type.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Recall breadth</dt>
          <dd>{current.configuration.top_k} passages</dd>
        </div>
      </dl>
    </aside>
  </div>
</section>

<Dialog
  open={rebuildReviewOpen}
  title="Publish structural retrieval changes?"
  description="The settings can be saved now, but existing vectors remain on the old structure until a full rebuild completes."
  onclose={() => {
    if (!saving) rebuildReviewOpen = false;
  }}
>
  <div class="dialog-copy">
    <p>The following structural values will change:</p>
    <ul>
      {#each structuralChanges as change (change)}
        <li>{change}</li>
      {/each}
    </ul>
    <p>
      Retrieval may be incomplete between publication and a successful rebuild.
    </p>
  </div>
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={saving}
      onclick={() => (rebuildReviewOpen = false)}
    >
      Keep current settings
    </Button>
    <Button
      disabled={saving}
      onclick={() => void publish(true)}
    >
      {saving ? "Publishing…" : "Publish changes"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={rebuildOpen}
  title="Rebuild the complete retrieval index?"
  description="This drops existing vectors and recalculates every document. Provider use and completion time depend on library size."
  onclose={() => {
    if (!rebuilding) rebuildOpen = false;
  }}
>
  <label class="confirmation-field" for="retrieval-rebuild-confirmation">
    Type <strong>REBUILD INDEX</strong> to continue
    <input
      id="retrieval-rebuild-confirmation"
      bind:value={confirmation}
      autocomplete="off"
      spellcheck="false"
    />
  </label>
  {#snippet footer()}
    <Button
      variant="quiet"
      onclick={() => (rebuildOpen = false)}
    >
      Cancel
    </Button>
    <Button
      variant="danger"
      disabled={confirmation.trim() !== "REBUILD INDEX"}
      onclick={() => void rebuildIndex()}
    >
      Start rebuild
    </Button>
  {/snippet}
</Dialog>

<style>
  .retrieval-panel {
    border-inline: 1px solid var(--color-rule-strong);
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .section-intro {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.6fr);
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.4rem;
  }

  .section-intro p,
  .section-intro h2,
  .section-intro > span {
    margin: 0;
  }

  .section-intro p,
  .maintenance-panel > p:first-child {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .section-intro h2 {
    margin-top: 0.25rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.4rem, 2.7vw, 2rem);
    font-weight: 520;
  }

  .section-intro > span {
    align-self: end;
    color: var(--color-ink-muted);
    font-size: 0.73rem;
    line-height: 1.6;
  }

  .panel-message,
  .configuration-warning {
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1.4rem;
    font-size: 0.7rem;
  }

  .panel-message {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--color-lichen);
  }

  .panel-message--error {
    color: var(--color-danger);
  }

  .configuration-warning {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.75rem;
    background: var(--color-clay-muted);
  }

  .configuration-warning strong,
  .configuration-warning p {
    margin: 0;
  }

  .configuration-warning p {
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .index-reading {
    display: grid;
    grid-template-columns: 0.25fr 0.25fr minmax(0, 0.5fr);
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .index-reading article {
    min-width: 0;
    display: grid;
    gap: 0.2rem;
    border-right: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
  }

  .index-reading article:last-child {
    border-right: 0;
  }

  .index-reading p,
  .index-reading span {
    overflow-wrap: anywhere;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.63rem;
  }

  .index-reading strong {
    overflow-wrap: anywhere;
    font-family: var(--font-editorial);
    font-size: 1.2rem;
    font-weight: 540;
  }

  .retrieval-workspace {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.3fr);
  }

  form {
    min-width: 0;
    border-right: 1px solid var(--color-rule-strong);
  }

  fieldset {
    display: grid;
    gap: 1rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    margin: 0;
    padding: clamp(1.15rem, 3vw, 1.8rem);
  }

  legend {
    padding: 0;
    font-family: var(--font-editorial);
    font-size: 1.2rem;
    font-weight: 540;
  }

  .field-grid,
  .number-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.8rem;
  }

  .number-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  label {
    display: grid;
    gap: 0.4rem;
    color: var(--color-ink-soft);
    font-size: 0.66rem;
    font-weight: 720;
  }

  label small {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    font-weight: 450;
    line-height: 1.45;
  }

  input,
  select {
    width: 100%;
    min-height: 2.65rem;
    border: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font: inherit;
    font-size: 0.74rem;
  }

  input:focus,
  select:focus {
    border-color: var(--color-focus);
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  input:disabled {
    opacity: 0.68;
  }

  .task-list {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border: 1px solid var(--color-rule);
  }

  .task-list label {
    min-width: 0;
    display: block;
    border-right: 1px solid var(--color-rule);
    padding: 0.75rem;
    cursor: pointer;
    background: var(--color-paper);
  }

  .task-list label:last-child {
    border-right: 0;
  }

  .task-list label.active {
    background: var(--color-clay-muted);
    box-shadow: inset 0 -3px 0 var(--color-clay);
  }

  .task-list input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
  }

  .task-list span {
    display: grid;
    gap: 0.28rem;
  }

  .task-list strong {
    color: var(--color-ink);
    font-size: 0.7rem;
  }

  .change-reading {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem 1.2rem;
    background: var(--color-clay-muted);
  }

  .change-reading strong,
  .change-reading p {
    margin: 0;
    font-size: 0.68rem;
  }

  .change-reading p {
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
  }

  form footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.9rem 1.15rem;
    background: var(--color-surface);
  }

  .preflight-status,
  .form-actions {
    display: flex;
    align-items: center;
    gap: 0.55rem;
  }

  .preflight-status span {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .maintenance-panel {
    min-width: 0;
    padding: clamp(1.2rem, 3vw, 1.8rem);
    background:
      linear-gradient(145deg, var(--color-clay-muted), transparent 60%),
      var(--color-surface);
  }

  .maintenance-panel > p:first-child,
  .maintenance-panel h3,
  .maintenance-panel > p:nth-of-type(2) {
    margin: 0;
  }

  .maintenance-panel h3 {
    margin-top: 0.3rem;
    font-family: var(--font-editorial);
    font-size: 1.4rem;
    font-weight: 520;
  }

  .maintenance-panel > p:nth-of-type(2) {
    margin: 0.65rem 0 1rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.55;
  }

  .rebuild-needed {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 0.8rem;
    color: var(--color-danger);
    font-size: 0.67rem;
    line-height: 1.45;
  }

  .rebuild-progress {
    margin-bottom: 0.8rem;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .rebuild-progress div {
    display: grid;
    gap: 0.25rem;
    padding: 0.75rem;
  }

  .rebuild-progress strong {
    font-family: var(--font-editorial);
    font-size: 1.3rem;
  }

  .rebuild-progress span {
    color: var(--color-ink-muted);
    font-size: 0.64rem;
    line-height: 1.4;
  }

  .rebuild-progress i {
    display: block;
    height: 4px;
    background: var(--color-clay);
    transition: width 180ms ease;
  }

  dl {
    margin: 1.4rem 0 0;
    border-block: 1px solid var(--color-rule);
  }

  dl div {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0;
    font-size: 0.64rem;
  }

  dl div:last-child {
    border-bottom: 0;
  }

  dt {
    color: var(--color-ink-muted);
  }

  dd {
    margin: 0;
    text-align: right;
    text-transform: capitalize;
  }

  .dialog-copy p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.75rem;
    line-height: 1.55;
  }

  .dialog-copy ul {
    margin: 0.8rem 0;
    padding-left: 1.2rem;
    font-size: 0.75rem;
  }

  .confirmation-field strong {
    color: var(--color-danger);
  }

  .confirmation-field input {
    margin-top: 0.3rem;
  }

  @media (max-width: 70rem) {
    .retrieval-workspace {
      grid-template-columns: 1fr;
    }

    form {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }

    .task-list {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .task-list label:nth-child(2) {
      border-right: 0;
    }

    .task-list label:nth-child(n + 3) {
      border-top: 1px solid var(--color-rule);
    }
  }

  @media (max-width: 48rem) {
    .section-intro,
    .field-grid,
    .number-grid {
      grid-template-columns: 1fr;
    }

    .section-intro {
      gap: 0.75rem;
    }

    .index-reading {
      grid-template-columns: 1fr;
    }

    .index-reading article,
    .index-reading article:last-child {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    form footer,
    .preflight-status {
      align-items: stretch;
      flex-direction: column;
    }

    .form-actions {
      justify-content: flex-end;
    }
  }
</style>
