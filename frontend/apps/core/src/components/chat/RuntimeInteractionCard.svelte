<script lang="ts">
  import type { FrontendRuntimeInteraction } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  interface Choice {
    id?: string;
    label?: string;
    value?: string;
    description?: string;
  }

  interface Props {
    interaction: FrontendRuntimeInteraction;
    busy: boolean;
    onresolve: (
      interaction: FrontendRuntimeInteraction,
      response: Readonly<Record<string, unknown>>,
    ) => Promise<void>;
  }

  let { interaction, busy, onresolve }: Props = $props();
  let answer = $state("");
  let note = $state("");
  let submitting = $state(false);
  let error = $state("");

  const request = $derived(interaction.request ?? {});
  const question = $derived(
    String(
      request.question ??
        request.message ??
        `The assistant needs your ${interaction.interaction_type.replaceAll("_", " ")}.`,
    ),
  );
  const choices = $derived(
    Array.isArray(request.choices) ? (request.choices as Choice[]) : [],
  );
  const isApproval = $derived(
    ["permission", "scope_expansion", "review_decision"].includes(
      interaction.interaction_type,
    ),
  );

  async function submit(response: Readonly<Record<string, unknown>>) {
    submitting = true;
    error = "";
    try {
      await onresolve(interaction, response);
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "The response could not be saved.";
    } finally {
      submitting = false;
    }
  }
</script>

<article class="interaction">
  <header>
    <span><Icon name={isApproval ? "shield" : "chat"} size={16} /></span>
    <div>
      <small>Response needed</small>
      <strong>{interaction.tool_name || interaction.interaction_type.replaceAll("_", " ")}</strong>
    </div>
  </header>
  <p>{question}</p>

  {#if choices.length}
    <div class="interaction__choices">
      {#each choices as choice, index (choice.id ?? choice.value ?? index)}
        <button
          type="button"
          disabled={busy || submitting}
          onclick={() =>
            submit({
              choice_id: choice.id,
              value: choice.value ?? choice.label,
            })}
        >
          <strong>{choice.label ?? choice.value ?? `Option ${index + 1}`}</strong>
          {#if choice.description}<small>{choice.description}</small>{/if}
        </button>
      {/each}
    </div>
    {#if request.allow_free_text}
      <div class="interaction__input">
        <input
          bind:value={answer}
          placeholder={String(request.input_placeholder ?? "Write another answer")}
          aria-label={String(request.input_label ?? "Answer")}
        />
        <button
          type="button"
          disabled={busy || submitting || !answer.trim()}
          onclick={() => submit({ text: answer })}
        >
          Answer
        </button>
      </div>
    {/if}
  {:else if isApproval}
    <textarea bind:value={note} rows="2" placeholder="Optional note"></textarea>
    <div class="interaction__decision">
      <button
        type="button"
        disabled={busy || submitting}
        onclick={() => submit({ decision: "deny", note })}
      >
        Decline
      </button>
      {#if interaction.interaction_type === "review_decision"}
        <button
          type="button"
          disabled={busy || submitting}
          onclick={() => submit({ decision: "request_changes", note })}
        >
          Request changes
        </button>
      {/if}
      <button
        class="approve"
        type="button"
        disabled={busy || submitting}
        onclick={() => submit({ decision: "approve", note })}
      >
        Approve & continue
      </button>
    </div>
  {:else}
    <div class="interaction__input">
      <input
        bind:value={answer}
        placeholder={String(request.input_placeholder ?? "Your answer")}
        aria-label={String(request.input_label ?? "Answer")}
      />
      <button
        type="button"
        disabled={busy || submitting || (!answer && request.required !== false)}
        onclick={() => submit({ value: answer })}
      >
        Answer & continue
      </button>
    </div>
  {/if}

  {#if error}<p class="interaction__error" role="alert">{error}</p>{/if}
</article>

<style>
  .interaction {
    width: min(50rem, calc(100% - 2rem));
    margin: 0 auto 0.7rem;
    border: 1px solid var(--color-tobacco);
    background: var(--color-tobacco-muted);
    box-shadow: 0 0.8rem 2rem color-mix(in srgb, var(--color-umber) 10%, transparent);
  }

  header {
    min-height: 3.5rem;
    display: flex;
    align-items: center;
    gap: 0.7rem;
    border-bottom: 1px solid color-mix(in srgb, var(--color-tobacco) 32%, transparent);
    padding: 0.55rem 0.75rem;
  }

  header > span {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-tobacco);
    color: var(--color-tobacco);
  }

  header div {
    display: grid;
    gap: 0.08rem;
  }

  header small {
    color: var(--color-tobacco);
    font-size: 0.54rem;
    font-weight: 750;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  header strong {
    font-family: var(--font-editorial);
    font-size: 0.95rem;
    font-weight: 600;
    text-transform: capitalize;
  }

  .interaction > p {
    margin: 0;
    padding: 0.8rem 0.85rem;
    color: var(--color-ink-soft);
    font-family: var(--font-editorial);
    font-size: 0.88rem;
    line-height: 1.5;
  }

  .interaction__choices {
    display: grid;
    gap: 1px;
    padding: 0 0.85rem 0.85rem;
  }

  .interaction__choices button {
    min-height: 2.8rem;
    display: grid;
    justify-items: start;
    gap: 0.1rem;
    border: 1px solid color-mix(in srgb, var(--color-tobacco) 35%, transparent);
    padding: 0.45rem 0.65rem;
    background: var(--color-surface);
    color: var(--color-ink);
    cursor: pointer;
    text-align: left;
  }

  .interaction__choices button:hover {
    border-color: var(--color-tobacco);
  }

  .interaction__choices strong {
    font-size: 0.7rem;
  }

  .interaction__choices small {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
  }

  textarea {
    width: calc(100% - 1.7rem);
    display: block;
    resize: vertical;
    border: 1px solid color-mix(in srgb, var(--color-tobacco) 35%, transparent);
    margin: 0 0.85rem 0.65rem;
    padding: 0.55rem;
    background: var(--color-surface);
    font-size: 0.7rem;
  }

  .interaction__input,
  .interaction__decision {
    display: flex;
    justify-content: flex-end;
    gap: 0.4rem;
    padding: 0 0.85rem 0.85rem;
  }

  .interaction__input input {
    min-width: 0;
    min-height: 2.35rem;
    flex: 1;
    border: 1px solid color-mix(in srgb, var(--color-tobacco) 35%, transparent);
    padding: 0 0.65rem;
    background: var(--color-surface);
    font-size: 0.7rem;
  }

  .interaction__input button,
  .interaction__decision button {
    min-height: 2.35rem;
    border: 1px solid var(--color-tobacco);
    padding: 0 0.75rem;
    background: var(--color-surface);
    color: var(--color-tobacco);
    cursor: pointer;
    font-size: 0.67rem;
    font-weight: 650;
  }

  .interaction__decision .approve,
  .interaction__input button {
    background: var(--color-tobacco);
    color: var(--color-surface);
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .interaction__error {
    border-top: 1px solid var(--color-danger);
    color: var(--color-danger) !important;
    font-family: var(--font-sans) !important;
    font-size: 0.65rem !important;
  }

  @media (max-width: 42rem) {
    .interaction {
      width: calc(100% - 0.75rem);
    }

    .interaction__decision {
      flex-wrap: wrap;
    }
  }
</style>
