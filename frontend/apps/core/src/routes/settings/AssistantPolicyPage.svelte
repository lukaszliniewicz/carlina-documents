<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendAssistantApprovalMode,
    FrontendAssistantPolicyDashboard,
    FrontendAssistantPolicyMutationResponse,
    FrontendAssistantPolicyUpdate,
  } from "@carlina/sdk";
  import {
    Badge,
    Button,
    Dialog,
    Icon,
    PageHeader,
  } from "@carlina/ui";

  import {
    assistantPolicyDraft,
    assistantPolicyDraftIsValid,
    assistantPolicyHasChanges,
    assistantPolicyNeedsFullAccessAcknowledgement,
    type AssistantPolicyDraft,
  } from "../../lib/assistant-policy.ts";

  interface Props {
    api: FrontendApiClient;
  }

  let { api }: Props = $props();

  let policy = $state<FrontendAssistantPolicyDashboard | null>(null);
  let draft = $state<AssistantPolicyDraft>({
    approvalMode: "review",
    maxToolCalls: 30,
    userHotLimit: 80,
    organizationHotLimit: 120,
    includeArchiveByDefault: false,
  });
  let loading = $state(true);
  let saving = $state(false);
  let error = $state("");
  let notice = $state("");
  let fullAccessReviewOpen = $state(false);
  let fullAccessConfirmation = $state("");

  const dirty = $derived(
    policy ? assistantPolicyHasChanges(policy, draft) : false,
  );
  const valid = $derived(
    policy ? assistantPolicyDraftIsValid(policy, draft) : false,
  );
  const fullAccessAcknowledgementNeeded = $derived(
    policy
      ? assistantPolicyNeedsFullAccessAcknowledgement(policy, draft)
      : false,
  );

  onMount(() => {
    void loadPolicy();
  });

  function applyPolicy(payload: FrontendAssistantPolicyDashboard): void {
    policy = payload;
    draft = assistantPolicyDraft(payload);
    fullAccessReviewOpen = false;
    fullAccessConfirmation = "";
  }

  async function loadPolicy(): Promise<void> {
    loading = true;
    error = "";
    notice = "";
    try {
      const payload =
        await api.request<FrontendAssistantPolicyDashboard>(
          "/api/frontend/settings/policy",
        );
      applyPolicy(payload);
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Assistant policy could not be loaded.";
    } finally {
      loading = false;
    }
  }

  function chooseApprovalMode(
    mode: FrontendAssistantApprovalMode,
  ): void {
    if (
      mode === "full_access" &&
      policy?.runtime.approval_mode !== "full_access"
    ) {
      fullAccessConfirmation = "";
      fullAccessReviewOpen = true;
      return;
    }
    draft.approvalMode = mode;
    notice = "";
  }

  function approveFullAccessDraft(): void {
    if (fullAccessConfirmation.trim() !== "FULL ACCESS") return;
    draft.approvalMode = "full_access";
    fullAccessReviewOpen = false;
    fullAccessConfirmation = "";
    notice =
      "Full access is selected but remains unpublished until you save.";
  }

  function discardChanges(): void {
    if (!policy) return;
    draft = assistantPolicyDraft(policy);
    error = "";
    notice = "Unsaved policy edits were discarded.";
  }

  function restoreDefaults(): void {
    if (!policy) return;
    draft = {
      approvalMode: policy.defaults.runtime.approval_mode,
      maxToolCalls: policy.defaults.runtime.max_tool_calls,
      userHotLimit: policy.defaults.memory.user_hot_limit,
      organizationHotLimit:
        policy.defaults.memory.organization_hot_limit,
      includeArchiveByDefault:
        policy.defaults.memory.include_archive_by_default,
    };
    notice =
      "Maintained defaults are prepared. Review them before saving.";
  }

  function setNumber(
    field: "maxToolCalls" | "userHotLimit" | "organizationHotLimit",
    value: number,
  ): void {
    draft[field] = value;
    notice = "";
  }

  function numberIsValid(
    value: number,
    minimum: number,
    maximum: number,
  ): boolean {
    return (
      Number.isInteger(value) &&
      value >= minimum &&
      value <= maximum
    );
  }

  async function savePolicy(): Promise<void> {
    if (!policy || !dirty || !valid) return;
    saving = true;
    error = "";
    notice = "";
    const update: FrontendAssistantPolicyUpdate = {
      runtime: {
        approval_mode: draft.approvalMode,
        max_tool_calls: draft.maxToolCalls,
      },
      memory: {
        user_hot_limit: draft.userHotLimit,
        organization_hot_limit: draft.organizationHotLimit,
        include_archive_by_default:
          draft.includeArchiveByDefault,
      },
    };
    if (fullAccessAcknowledgementNeeded) {
      update.acknowledge_full_access = true;
    }
    try {
      const response =
        await api.request<FrontendAssistantPolicyMutationResponse>(
          policy.actions.update_url,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(update),
          },
        );
      applyPolicy(response.policy);
      notice = response.message || "Assistant policy updated.";
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Assistant policy was not saved.";
    } finally {
      saving = false;
    }
  }
</script>

<div class="policy-page">
  <PageHeader
    eyebrow="Settings / Assistant policy"
    title="Pace, permission, memory"
    description="Set the shared operating posture for new assistant turns: how far a turn may run, when side effects pause for review, and how much durable memory stays close at hand."
  >
    {#snippet actions()}
      <Button
        variant="outline"
        onclick={restoreDefaults}
        disabled={!policy || saving}
      >
        <Icon name="refresh" size={14} />
        Use defaults
      </Button>
      <Button
        onclick={() => void savePolicy()}
        disabled={!dirty || !valid || saving}
      >
        <Icon name="check" size={15} />
        {saving ? "Saving…" : "Save policy"}
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
      aria-label="Loading assistant policy"
      aria-busy="true"
    >
      <span></span><span></span><span></span>
    </section>
  {:else if policy}
    {#if policy.summary.configuration_needs_review}
      <section class="policy-warning" role="alert">
        <Icon name="activity" size={18} />
        <div>
          <strong>The stored tool budget is outside this desk’s safe range.</strong>
          <p>
            Choose a whole number from
            {policy.limits.max_tool_calls.minimum} to
            {policy.limits.max_tool_calls.maximum} before publishing.
            Runtime execution is currently capped at
            {policy.runtime.effective_max_tool_calls}; the stored value is not
            changed until you save.
          </p>
        </div>
      </section>
    {/if}

    <section class="summary-strip" aria-label="Assistant policy summary">
      <article>
        <p>Execution posture</p>
        <strong>
          {draft.approvalMode === "review" ? "Review first" : "Full access"}
        </strong>
        <span>
          {draft.approvalMode === "review"
            ? "side effects pause"
            : "side effects proceed"}
        </span>
      </article>
      <article>
        <p>Turn budget</p>
        <strong>{Number.isFinite(draft.maxToolCalls) ? draft.maxToolCalls : "—"}</strong>
        <span>tool-call iterations</span>
      </article>
      <article>
        <p>Hot memory</p>
        <strong>{Number.isFinite(draft.userHotLimit) ? draft.userHotLimit : "—"}</strong>
        <span>notes per member</span>
      </article>
      <article>
        <p>Archive recall</p>
        <strong>
          {draft.includeArchiveByDefault ? "Included" : "On request"}
        </strong>
        <span>for ordinary searches</span>
      </article>
    </section>

    <div class="policy-layout">
      <section class="policy-panel runtime-panel">
        <header class="panel-heading">
          <div>
            <p>Runtime boundaries</p>
            <h2>How a turn proceeds</h2>
          </div>
          <Badge
            tone={draft.approvalMode === "review" ? "success" : "warning"}
          >
            {draft.approvalMode === "review" ? "Review first" : "Full access"}
          </Badge>
        </header>

        <div class="budget-field">
          <div>
            <label for="policy-max-tool-calls">Tool-call budget</label>
            <p>
              Maximum consecutive assistant tool iterations before the turn
              must return control to the conversation.
            </p>
          </div>
          <input
            id="policy-max-tool-calls"
            type="number"
            min={policy.limits.max_tool_calls.minimum}
            max={policy.limits.max_tool_calls.maximum}
            step="1"
            value={draft.maxToolCalls}
            aria-invalid={!numberIsValid(
              draft.maxToolCalls,
              policy.limits.max_tool_calls.minimum,
              policy.limits.max_tool_calls.maximum,
            )}
            oninput={(event) =>
              setNumber("maxToolCalls", event.currentTarget.valueAsNumber)}
          />
        </div>
        <p
          class:error-copy={!numberIsValid(
            draft.maxToolCalls,
            policy.limits.max_tool_calls.minimum,
            policy.limits.max_tool_calls.maximum,
          )}
          class="range-copy"
        >
          Whole numbers from {policy.limits.max_tool_calls.minimum} to
          {policy.limits.max_tool_calls.maximum}. The maintained default is
          {policy.defaults.runtime.max_tool_calls}.
        </p>

        <fieldset>
          <legend>Side-effect approval</legend>
          <p class="field-intro">
            Choose whether actions such as writes and uploads pause for a
            person to review them.
          </p>
          <div
            class="approval-options"
            role="radiogroup"
            aria-label="Side-effect approval posture"
          >
            <button
              type="button"
              role="radio"
              aria-checked={draft.approvalMode === "review"}
              class:selected={draft.approvalMode === "review"}
              onclick={() => chooseApprovalMode("review")}
            >
              <span class="choice-index">A</span>
              <span>
                <strong>Review first</strong>
                <small>
                  Pause before tools perform consequential side effects.
                  Recommended for everyday work.
                </small>
              </span>
              <span class="choice-mark" aria-hidden="true"></span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={draft.approvalMode === "full_access"}
              class:selected={draft.approvalMode === "full_access"}
              class:choice-danger={draft.approvalMode === "full_access"}
              onclick={() => chooseApprovalMode("full_access")}
            >
              <span class="choice-index">B</span>
              <span>
                <strong>Full access</strong>
                <small>
                  Let side-effecting tools proceed without the ordinary
                  approval pause.
                </small>
              </span>
              <span class="choice-mark" aria-hidden="true"></span>
            </button>
          </div>
        </fieldset>

        <div class="boundary-note">
          <Icon name="shield" size={17} />
          <p>
            <strong>Capability scope does not change.</strong>
            Resource references, account boundaries, and server authorization
            continue to apply in either posture.
          </p>
        </div>
      </section>

      <section class="policy-panel memory-panel">
        <header class="panel-heading">
          <div>
            <p>Memory shelf</p>
            <h2>What stays close</h2>
          </div>
          <Badge tone="neutral">Two tiers</Badge>
        </header>

        <div class="tier-map" aria-label="Memory tier explanation">
          <article>
            <span>01 / Hot</span>
            <strong>Ready context</strong>
            <p>
              Higher-priority notes kept in the assistant’s regular recall
              set.
            </p>
          </article>
          <div class="tier-arrow" aria-hidden="true">
            <span></span>
            <Icon name="arrow-right" size={16} />
          </div>
          <article>
            <span>02 / Archive</span>
            <strong>Searchable reserve</strong>
            <p>
              Older or lower-priority notes retained for deliberate retrieval.
            </p>
          </article>
        </div>

        <div class="memory-limits">
          <label>
            <span>
              <strong>Member hot shelf</strong>
              <small>Maximum active notes for each member.</small>
            </span>
            <input
              type="number"
              min={policy.limits.hot_memory.minimum}
              max={policy.limits.hot_memory.maximum}
              step="1"
              value={draft.userHotLimit}
              aria-invalid={!numberIsValid(
                draft.userHotLimit,
                policy.limits.hot_memory.minimum,
                policy.limits.hot_memory.maximum,
              )}
              oninput={(event) =>
                setNumber("userHotLimit", event.currentTarget.valueAsNumber)}
            />
          </label>
          <label>
            <span>
              <strong>Organization hot shelf</strong>
              <small>Maximum active shared organization notes.</small>
            </span>
            <input
              type="number"
              min={policy.limits.hot_memory.minimum}
              max={policy.limits.hot_memory.maximum}
              step="1"
              value={draft.organizationHotLimit}
              aria-invalid={!numberIsValid(
                draft.organizationHotLimit,
                policy.limits.hot_memory.minimum,
                policy.limits.hot_memory.maximum,
              )}
              oninput={(event) =>
                setNumber(
                  "organizationHotLimit",
                  event.currentTarget.valueAsNumber,
                )}
            />
          </label>
        </div>
        <p
          class:error-copy={!numberIsValid(
            draft.userHotLimit,
            policy.limits.hot_memory.minimum,
            policy.limits.hot_memory.maximum,
          ) || !numberIsValid(
            draft.organizationHotLimit,
            policy.limits.hot_memory.minimum,
            policy.limits.hot_memory.maximum,
          )}
          class="range-copy"
        >
          Both shelves accept {policy.limits.hot_memory.minimum}–
          {policy.limits.hot_memory.maximum} notes.
        </p>

        <label class="archive-choice">
          <input
            type="checkbox"
            bind:checked={draft.includeArchiveByDefault}
          />
          <span class="switch-mark" aria-hidden="true"></span>
          <span>
            <strong>Search the archive by default</strong>
            <small>
              Ordinary recall includes tier 2. Turn this off for a tighter,
              faster default search over the hot shelf.
            </small>
          </span>
        </label>

        <div class="category-index">
          <div>
            <p>Fallback vocabulary</p>
            <span>
              Personal category labels are managed in Profile. These are the
              maintained fallbacks when no personal edition is present.
            </span>
          </div>
          <ul>
            {#each policy.memory.fallback_categories as category (category.key)}
              <li>{category.label}</li>
            {/each}
          </ul>
        </div>
      </section>
    </div>

    <footer class="save-ledger">
      <div>
        <span class:changed={dirty}></span>
        <p>
          <strong>
            {dirty
              ? valid
                ? "Unpublished policy edits"
                : "A value needs attention"
              : "Assistant policy is current"}
          </strong>
          <small>
            Saved changes apply to new turns and future memory recall.
          </small>
        </p>
      </div>
      <div class="ledger-actions">
        <Button
          variant="quiet"
          onclick={discardChanges}
          disabled={!dirty || saving}
        >
          Discard
        </Button>
        <Button
          onclick={() => void savePolicy()}
          disabled={!dirty || !valid || saving}
        >
          {saving ? "Publishing policy…" : "Publish policy changes"}
        </Button>
      </div>
    </footer>
  {:else}
    <section class="unavailable-state">
      <Icon name="shield" size={24} />
      <h2>Policy desk unavailable</h2>
      <p>
        This administrator-only surface could not be opened with the current
        session.
      </p>
      <Button variant="outline" onclick={() => void loadPolicy()}>
        Try again
      </Button>
    </section>
  {/if}
</div>

<Dialog
  open={fullAccessReviewOpen}
  title="Switch to full access?"
  description="This removes the ordinary approval pause for side-effecting tools across new assistant turns."
  onclose={() => {
    if (!saving) {
      fullAccessReviewOpen = false;
      fullAccessConfirmation = "";
    }
  }}
  size="sm"
>
  <div class="full-access-review">
    <div class="review-impact">
      <Icon name="activity" size={18} />
      <p>
        File writes, uploads, external updates, and similar effects may proceed
        without an individual review prompt. Existing capability and resource
        permissions remain enforced.
      </p>
    </div>
    <label>
      <span>Type <code>FULL ACCESS</code> to prepare this change</span>
      <input
        type="text"
        autocomplete="off"
        spellcheck="false"
        bind:value={fullAccessConfirmation}
      />
    </label>
    <p>
      This dialog only prepares the selection. The policy is not changed until
      you use Save policy.
    </p>
  </div>
  {#snippet footer()}
    <Button
      variant="quiet"
      onclick={() => {
        fullAccessReviewOpen = false;
        fullAccessConfirmation = "";
      }}
    >
      Keep review first
    </Button>
    <Button
      variant="danger"
      disabled={fullAccessConfirmation.trim() !== "FULL ACCESS"}
      onclick={approveFullAccessDraft}
    >
      Prepare full access
    </Button>
  {/snippet}
</Dialog>

<style>
  .policy-page {
    width: min(100%, 100rem);
    min-height: 100%;
    margin: 0 auto;
    padding: clamp(1.5rem, 3vw, 3rem) clamp(1rem, 4vw, 3.5rem) 4rem;
  }

  .desk-message {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    margin: 0 0 1rem;
    border: 1px solid var(--color-lichen);
    padding: 0.72rem 0.85rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.78rem;
  }

  .desk-message--error {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .loading-state {
    min-height: 24rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    border-top: 1px solid var(--color-rule);
  }

  .loading-state span {
    width: 0.42rem;
    height: 2.4rem;
    background: var(--color-clay-muted);
    animation: ledger-pulse 1.1s ease-in-out infinite;
  }

  .loading-state span:nth-child(2) {
    animation-delay: 130ms;
  }

  .loading-state span:nth-child(3) {
    animation-delay: 260ms;
  }

  .policy-warning {
    display: flex;
    align-items: flex-start;
    gap: 0.8rem;
    margin-bottom: 1rem;
    border: 1px solid var(--color-tobacco);
    padding: 0.9rem 1rem;
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
  }

  .policy-warning strong,
  .policy-warning p {
    margin: 0;
  }

  .policy-warning p {
    margin-top: 0.25rem;
    font-size: 0.78rem;
    line-height: 1.5;
  }

  .summary-strip {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-block: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .summary-strip article {
    min-height: 7.2rem;
    display: flex;
    flex-direction: column;
    justify-content: center;
    border-inline-end: 1px solid var(--color-rule);
    padding: 1rem 1.2rem;
  }

  .summary-strip article:last-child {
    border-inline-end: 0;
  }

  .summary-strip p,
  .panel-heading p,
  .category-index > div > p {
    margin: 0 0 0.5rem;
    color: var(--color-clay);
    font-size: 0.61rem;
    font-weight: 800;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  .summary-strip strong {
    font-family: var(--font-editorial);
    font-size: clamp(1.35rem, 2vw, 1.8rem);
    font-weight: 500;
  }

  .summary-strip span {
    margin-top: 0.28rem;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
  }

  .policy-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    border-bottom: 1px solid var(--color-rule-strong);
  }

  .policy-panel {
    padding: clamp(1.4rem, 2.8vw, 2.4rem);
    background: var(--color-surface);
  }

  .policy-panel + .policy-panel {
    border-inline-start: 1px solid var(--color-rule-strong);
    background: color-mix(
      in srgb,
      var(--color-paper) 56%,
      var(--color-surface)
    );
  }

  .panel-heading {
    min-height: 4.5rem;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 1rem;
  }

  .panel-heading h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.55rem, 2.5vw, 2.15rem);
    font-weight: 500;
  }

  .budget-field {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 6rem;
    align-items: center;
    gap: 1.2rem;
    padding: 1.5rem 0 0.3rem;
  }

  .budget-field label,
  fieldset legend {
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 1.05rem;
    font-weight: 600;
  }

  .budget-field p,
  .field-intro {
    margin: 0.3rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.5;
  }

  input[type="number"],
  .full-access-review input {
    width: 100%;
    min-height: 2.65rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.55rem 0.7rem;
    background: var(--color-paper);
    color: var(--color-ink);
  }

  input[type="number"] {
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 1rem;
    text-align: right;
  }

  input[aria-invalid="true"] {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
  }

  .range-copy {
    margin: 0 0 1.5rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .range-copy.error-copy {
    color: var(--color-danger);
  }

  fieldset {
    margin: 0;
    border: 0;
    border-top: 1px solid var(--color-rule);
    padding: 1.35rem 0 0;
  }

  fieldset legend {
    padding: 0;
  }

  .approval-options {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin-top: 0.85rem;
    border: 1px solid var(--color-rule);
  }

  .approval-options button {
    min-height: 8.4rem;
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: start;
    gap: 0.75rem;
    border: 0;
    padding: 1rem;
    background: transparent;
    color: var(--color-ink);
    cursor: pointer;
    text-align: left;
  }

  .approval-options button + button {
    border-inline-start: 1px solid var(--color-rule);
  }

  .approval-options button:hover {
    background: var(--color-surface-muted);
  }

  .approval-options button.selected {
    box-shadow: inset 0 -3px 0 var(--color-lichen);
    background: var(--color-lichen-muted);
  }

  .approval-options button.choice-danger {
    box-shadow: inset 0 -3px 0 var(--color-tobacco);
    background: var(--color-tobacco-muted);
  }

  .choice-index {
    color: var(--color-rule-strong);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.65rem;
  }

  .approval-options strong,
  .approval-options small {
    display: block;
  }

  .approval-options strong {
    font-family: var(--font-editorial);
    font-size: 1rem;
  }

  .approval-options small {
    margin-top: 0.45rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.5;
  }

  .choice-mark {
    width: 0.62rem;
    height: 0.62rem;
    border: 1px solid var(--color-rule-strong);
    margin-top: 0.2rem;
  }

  .selected .choice-mark {
    border-color: var(--color-lichen);
    background: var(--color-lichen);
  }

  .choice-danger .choice-mark {
    border-color: var(--color-tobacco);
    background: var(--color-tobacco);
  }

  .boundary-note {
    display: flex;
    align-items: flex-start;
    gap: 0.7rem;
    margin-top: 1rem;
    border-inline-start: 3px solid var(--color-clay);
    padding: 0.8rem 0.9rem;
    background: var(--color-clay-muted);
    color: var(--color-ink-soft);
  }

  .boundary-note p {
    margin: 0;
    font-size: 0.72rem;
    line-height: 1.5;
  }

  .boundary-note strong {
    display: block;
    margin-bottom: 0.15rem;
  }

  .tier-map {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 2.7rem minmax(0, 1fr);
    align-items: stretch;
    padding: 1.5rem 0;
  }

  .tier-map article {
    border-block: 1px solid var(--color-rule);
    padding: 1rem 0.85rem;
    background: var(--color-surface);
  }

  .tier-map article > span {
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.6rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .tier-map strong {
    display: block;
    margin-top: 0.6rem;
    font-family: var(--font-editorial);
    font-size: 1rem;
  }

  .tier-map p {
    margin: 0.35rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
    line-height: 1.45;
  }

  .tier-arrow {
    display: grid;
    place-items: center;
    color: var(--color-rule-strong);
  }

  .tier-arrow span {
    width: 100%;
    height: 1px;
    grid-area: 1 / 1;
    background: var(--color-rule);
  }

  .tier-arrow :global(svg) {
    z-index: 1;
    grid-area: 1 / 1;
    background: color-mix(
      in srgb,
      var(--color-paper) 56%,
      var(--color-surface)
    );
  }

  .memory-limits {
    border-top: 1px solid var(--color-rule);
  }

  .memory-limits label {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 6rem;
    align-items: center;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem 0;
  }

  .memory-limits strong,
  .memory-limits small {
    display: block;
  }

  .memory-limits strong {
    font-family: var(--font-editorial);
    font-size: 0.95rem;
  }

  .memory-limits small {
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .memory-panel .range-copy {
    margin-top: 0.5rem;
  }

  .archive-choice {
    display: grid;
    grid-template-columns: 2.2rem minmax(0, 1fr);
    align-items: start;
    gap: 0.75rem;
    border: 1px solid var(--color-rule-strong);
    padding: 1rem;
    background: var(--color-surface);
    cursor: pointer;
  }

  .archive-choice input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .switch-mark {
    width: 2.1rem;
    height: 1.05rem;
    position: relative;
    border: 1px solid var(--color-rule-strong);
    margin-top: 0.15rem;
    background: var(--color-surface-sunken);
  }

  .switch-mark::after {
    content: "";
    width: 0.65rem;
    height: 0.65rem;
    position: absolute;
    top: 0.13rem;
    left: 0.16rem;
    background: var(--color-ink-muted);
    transition: transform var(--shell-transition);
  }

  .archive-choice input:checked + .switch-mark {
    border-color: var(--color-lichen);
    background: var(--color-lichen-muted);
  }

  .archive-choice input:checked + .switch-mark::after {
    background: var(--color-lichen);
    transform: translateX(1rem);
  }

  .archive-choice input:focus-visible + .switch-mark {
    outline: 2px solid var(--color-focus);
    outline-offset: 3px;
  }

  .archive-choice strong,
  .archive-choice small {
    display: block;
  }

  .archive-choice strong {
    font-family: var(--font-editorial);
    font-size: 0.96rem;
  }

  .archive-choice small {
    margin-top: 0.3rem;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
    line-height: 1.45;
  }

  .category-index {
    margin-top: 1.2rem;
    border-top: 1px solid var(--color-rule);
    padding-top: 1rem;
  }

  .category-index > div > span {
    display: block;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.5;
  }

  .category-index ul {
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    margin: 0.8rem 0 0;
    padding: 0;
    list-style: none;
  }

  .category-index li {
    border: 1px solid var(--color-rule);
    margin: -1px 0 0 -1px;
    padding: 0.38rem 0.55rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-size: 0.65rem;
  }

  .save-ledger {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.2rem;
    background: var(--color-surface-muted);
  }

  .save-ledger > div:first-child {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .save-ledger > div:first-child > span {
    width: 0.68rem;
    height: 0.68rem;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .save-ledger > div:first-child > span.changed {
    border-color: var(--color-clay);
    background: var(--color-clay);
  }

  .save-ledger p,
  .save-ledger strong,
  .save-ledger small {
    margin: 0;
  }

  .save-ledger strong,
  .save-ledger small {
    display: block;
  }

  .save-ledger strong {
    font-family: var(--font-editorial);
    font-size: 0.92rem;
  }

  .save-ledger small {
    margin-top: 0.16rem;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
  }

  .ledger-actions {
    display: flex;
    gap: 0.45rem;
  }

  .unavailable-state {
    min-height: 24rem;
    display: grid;
    place-content: center;
    justify-items: center;
    border-top: 1px solid var(--color-rule);
    text-align: center;
  }

  .unavailable-state h2 {
    margin: 0.8rem 0 0.25rem;
    font-family: var(--font-editorial);
    font-weight: 500;
  }

  .unavailable-state p {
    max-width: 28rem;
    margin: 0 0 1rem;
    color: var(--color-ink-muted);
    font-size: 0.78rem;
    line-height: 1.5;
  }

  .full-access-review {
    display: grid;
    gap: 1rem;
  }

  .review-impact {
    display: flex;
    align-items: flex-start;
    gap: 0.7rem;
    border-inline-start: 3px solid var(--color-danger);
    padding: 0.8rem;
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .review-impact p,
  .full-access-review > p {
    margin: 0;
    font-size: 0.74rem;
    line-height: 1.5;
  }

  .full-access-review label span {
    display: block;
    margin-bottom: 0.4rem;
    font-size: 0.74rem;
    font-weight: 650;
  }

  .full-access-review code {
    border: 1px solid var(--color-rule);
    padding: 0.12rem 0.28rem;
    background: var(--color-paper);
    font-size: 0.7rem;
  }

  .full-access-review > p {
    color: var(--color-ink-muted);
  }

  @keyframes ledger-pulse {
    0%,
    100% {
      opacity: 0.3;
      transform: scaleY(0.55);
    }
    50% {
      opacity: 1;
      transform: scaleY(1);
    }
  }

  @media (max-width: 74rem) {
    .policy-layout {
      grid-template-columns: 1fr;
    }

    .policy-panel + .policy-panel {
      border-inline-start: 0;
      border-top: 1px solid var(--color-rule-strong);
    }
  }

  @media (max-width: 54rem) {
    .summary-strip {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .summary-strip article:nth-child(2) {
      border-inline-end: 0;
    }

    .summary-strip article:nth-child(-n + 2) {
      border-bottom: 1px solid var(--color-rule);
    }
  }

  @media (max-width: 38rem) {
    .policy-page {
      padding-inline: 0.8rem;
    }

    .summary-strip {
      grid-template-columns: 1fr;
    }

    .summary-strip article {
      min-height: 5.8rem;
      border-inline-end: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .summary-strip article:last-child {
      border-bottom: 0;
    }

    .approval-options {
      grid-template-columns: 1fr;
    }

    .approval-options button + button {
      border-inline-start: 0;
      border-top: 1px solid var(--color-rule);
    }

    .tier-map {
      grid-template-columns: 1fr;
    }

    .tier-arrow {
      min-height: 2rem;
      transform: rotate(90deg);
    }

    .save-ledger {
      align-items: stretch;
      flex-direction: column;
    }

    .ledger-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
