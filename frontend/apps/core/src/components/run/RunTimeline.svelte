<script lang="ts">
  import type { FrontendRunStepSummary } from "@carlina/sdk";
  import StatusMark from "@carlina/runtime-ui/status-mark";

  import { formatRunDate, formatRunDuration } from "../../lib/run-studio.ts";

  interface Props {
    steps: FrontendRunStepSummary[];
    selectedId: string | null;
    mode: "timeline" | "steps";
    onselect: (stepId: string) => void;
  }

  let { steps, selectedId, mode, onselect }: Props = $props();
  const titles = $derived(
    new Map(steps.map((step) => [step.id, step.title])),
  );
</script>

<section class:step-list={mode === "steps"} class:timeline={mode === "timeline"}>
  <header>
    <div>
      <small>{mode === "timeline" ? "Execution order" : "Dependency-ordered list"}</small>
      <strong>{mode === "timeline" ? "Run timeline" : "All workflow steps"}</strong>
    </div>
    <span>{steps.length} steps</span>
  </header>

  {#if steps.length}
    <ol>
      {#each steps as step, index (step.id)}
        <li class:selected={selectedId === step.id}>
          <button type="button" onclick={() => onselect(step.id)}>
            <span class="sequence">{String(step.sequence ?? index + 1).padStart(2, "0")}</span>
            <span class="step-copy">
              <span class="step-leading">
                <strong>{step.title}</strong>
                <StatusMark status={step.status} label={step.status_label} compact />
              </span>
              <span class="purpose">{step.purpose}</span>
              {#if step.dependency_ids.length}
                <span class="dependencies">
                  After
                  {step.dependency_ids.map((id) => titles.get(id) ?? id).join(", ")}
                </span>
              {/if}
            </span>
            <span class="step-time">
              <strong>{formatRunDuration(step.duration_ms)}</strong>
              <span>{formatRunDate(step.started_at)}</span>
            </span>
          </button>
        </li>
      {/each}
    </ol>
  {:else}
    <p class="empty">This run does not have compiled steps yet.</p>
  {/if}
</section>

<style>
  section {
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  section > header {
    min-height: 4rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem 0.9rem;
  }

  header > div {
    display: grid;
    gap: 0.18rem;
  }

  header small {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 780;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  header strong {
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
  }

  header > span {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  ol {
    display: grid;
    gap: 1px;
    margin: 0;
    padding: 0;
    background: var(--color-rule);
    list-style: none;
  }

  li {
    background: var(--color-surface);
  }

  li.selected {
    box-shadow: inset 3px 0 var(--color-focus);
  }

  button {
    width: 100%;
    min-height: 6.2rem;
    display: grid;
    grid-template-columns: 2.2rem minmax(0, 1fr) auto;
    align-items: start;
    gap: 0.75rem;
    border: 0;
    padding: 0.85rem 0.9rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  button:hover {
    background: var(--color-surface-muted);
  }

  .sequence {
    padding-top: 0.1rem;
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.9rem;
  }

  .step-copy,
  .step-time {
    display: grid;
  }

  .step-copy {
    min-width: 0;
    gap: 0.3rem;
  }

  .step-leading {
    display: flex;
    align-items: center;
    gap: 0.55rem;
  }

  .step-leading > strong {
    min-width: 0;
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.94rem;
    font-weight: 560;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .purpose {
    color: var(--color-ink-soft);
    font-size: 0.67rem;
    line-height: 1.5;
  }

  .dependencies {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
  }

  .step-time {
    justify-items: end;
    gap: 0.22rem;
    color: var(--color-ink-muted);
    font-size: 0.56rem;
    text-align: right;
  }

  .step-time strong {
    color: var(--color-ink-soft);
    font-size: 0.63rem;
  }

  .empty {
    margin: 0;
    padding: 2rem 0.9rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  @media (max-width: 42rem) {
    button {
      grid-template-columns: 1.8rem minmax(0, 1fr);
    }

    .step-time {
      display: none;
    }
  }
</style>
