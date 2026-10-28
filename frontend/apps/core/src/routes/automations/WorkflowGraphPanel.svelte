<script lang="ts">
  import { type Component } from "svelte";
  import type {
    FrontendAutomationDefinition,
    FrontendRunGraph,
  } from "@carlina/sdk";
  import { EmptyState, Icon } from "@carlina/ui";

  import { automationDefinitionGraph } from "../../lib/automations.ts";

  interface GraphProps {
    graph: FrontendRunGraph;
    selectedId: string | null;
    onselect: (stepId: string) => void;
  }

  interface Props {
    definition: FrontendAutomationDefinition;
    selectedKey?: string | null;
    onselect?: (stepKey: string) => void;
    eyebrow?: string;
    title?: string;
  }

  let {
    definition,
    selectedKey = null,
    onselect = () => {},
    eyebrow = "Structure",
    title = "Workflow graph",
  }: Props = $props();

  let Graph = $state<Component<GraphProps> | null>(null);
  let graphLoadError = $state("");
  const graph = $derived(automationDefinitionGraph(definition));

  $effect(() => {
    if (!graph.nodes.length || Graph || graphLoadError) return;
    let current = true;
    void import("@carlina/runtime-ui/graph")
      .then((module) => {
        if (current) Graph = module.default;
      })
      .catch((reason: unknown) => {
        if (current) {
          graphLoadError =
            reason instanceof Error
              ? reason.message
              : "The graph renderer could not be loaded.";
        }
      });
    return () => {
      current = false;
    };
  });
</script>

<section class="graph-panel">
  <header>
    <div>
      <p>{eyebrow}</p>
      <h2>{title}</h2>
    </div>
    <span>{graph.nodes.length.toString().padStart(2, "0")} steps</span>
  </header>

  {#if !graph.nodes.length}
    <EmptyState
      icon="activity"
      title="No steps to preview"
      description="Add the first step to begin shaping this automation."
    />
  {:else if Graph}
    <div class="graph-stage">
      <Graph
        {graph}
        selectedId={selectedKey}
        {onselect}
      />
    </div>
  {:else if graphLoadError}
    <div class="graph-error" role="alert">
      <Icon name="activity" size={20} />
      <div>
        <strong>Graph unavailable</strong>
        <p>{graphLoadError}</p>
      </div>
    </div>
  {:else}
    <div class="graph-loading" aria-busy="true">
      <span></span>
      <span></span>
      <span></span>
      <p>Preparing the dependency graph…</p>
    </div>
  {/if}
</section>

<style>
  .graph-panel {
    min-width: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .graph-panel > header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.78rem 0.9rem;
  }

  header div {
    display: grid;
    gap: 0.12rem;
  }

  header p,
  header h2,
  header span {
    margin: 0;
  }

  header p,
  header span {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 730;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  header h2 {
    font-family: var(--font-editorial);
    font-size: 1.08rem;
    font-weight: 540;
  }

  header span {
    color: var(--color-clay);
    font-family: var(--font-editorial);
  }

  .graph-stage {
    min-height: 29rem;
    overflow: hidden;
  }

  .graph-stage :global(.run-graph) {
    min-height: 29rem;
    border: 0;
    grid-template-rows: auto auto minmax(25rem, 1fr);
  }

  .graph-stage :global(.flow-stage) {
    min-height: 25rem;
  }

  .graph-loading,
  .graph-error {
    min-height: 29rem;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 0.65rem;
    padding: 2rem;
    text-align: center;
  }

  .graph-loading {
    grid-template-columns: repeat(3, 2.3rem);
  }

  .graph-loading span {
    height: 5rem;
    border: 1px solid var(--color-rule);
    background: var(--color-surface-muted);
    animation: graph-pulse 1.2s ease-in-out infinite;
  }

  .graph-loading span:nth-child(2) {
    animation-delay: 120ms;
  }

  .graph-loading span:nth-child(3) {
    animation-delay: 240ms;
  }

  .graph-loading p {
    grid-column: 1 / -1;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .graph-error {
    grid-template-columns: auto minmax(0, 22rem);
    justify-items: start;
    text-align: left;
  }

  .graph-error strong {
    font-family: var(--font-editorial);
    font-weight: 560;
  }

  .graph-error p {
    margin: 0.25rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.5;
  }

  @keyframes graph-pulse {
    50% {
      background: var(--color-clay-muted);
    }
  }

  @media (max-width: 48rem) {
    .graph-stage,
    .graph-loading,
    .graph-error {
      min-height: 24rem;
    }

    .graph-stage :global(.run-graph) {
      min-height: 24rem;
    }
  }
</style>
