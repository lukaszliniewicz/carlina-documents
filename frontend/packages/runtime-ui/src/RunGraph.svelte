<script lang="ts">
  import { onDestroy } from "svelte";
  import type { FrontendRunGraph } from "@carlina/sdk";
  import { Button } from "@carlina/ui";
  import {
    Background,
    BackgroundVariant,
    Controls,
    MiniMap,
    SvelteFlow,
    type Edge,
    type Node,
  } from "@xyflow/svelte";
  import "@xyflow/svelte/dist/style.css";

  import RunNode from "./RunNode.svelte";
  import {
    dependencyNeighborhood,
    fallbackRunGraphPositions,
    type RunGraphPosition,
  } from "./run-graph.ts";

  interface Props {
    graph: FrontendRunGraph;
    selectedId: string | null;
    onselect: (stepId: string) => void;
  }

  let { graph, selectedId, onselect }: Props = $props();
  let container: HTMLElement;
  let layoutError = $state("");
  let layingOut = $state(false);
  let fullscreen = $state(false);
  let focusBranch = $state(false);
  let worker: Worker | null = null;
  let lastLayoutSignature = "";
  const positions = new Map<string, RunGraphPosition>();
  let nodes = $state.raw<Node[]>([]);
  let edges = $state.raw<Edge[]>([]);
  const nodeTypes = { run: RunNode };

  function durationLabel(milliseconds: number | null): string {
    if (milliseconds === null) return "Not started";
    const seconds = Math.max(0, Math.round(milliseconds / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return `${minutes}m ${remainder}s`;
  }

  function displayedGraph(): FrontendRunGraph {
    if (!focusBranch || !selectedId) return graph;
    const neighborhood = dependencyNeighborhood(graph, selectedId);
    const visibleIds = new Set([
      selectedId,
      ...neighborhood.upstream,
      ...neighborhood.downstream,
    ]);
    return {
      ...graph,
      nodes: graph.nodes.filter((node) => visibleIds.has(node.id)),
      edges: graph.edges.filter(
        (edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target),
      ),
    };
  }

  function renderGraph() {
    const visibleGraph = displayedGraph();
    const neighborhood = dependencyNeighborhood(visibleGraph, selectedId);
    const fallback = fallbackRunGraphPositions(visibleGraph);
    for (const position of fallback) {
      if (!positions.has(position.id)) positions.set(position.id, position);
    }
    nodes =
      visibleGraph.nodes.map((step) => {
        const position = positions.get(step.id) ?? { id: step.id, x: 0, y: 0 };
        const relation = selectedId === step.id
          ? "selected"
          : neighborhood.upstream.has(step.id)
            ? "upstream"
            : neighborhood.downstream.has(step.id)
              ? "downstream"
              : selectedId
                ? "unrelated"
                : "";
        return {
          id: step.id,
          type: "run",
          position: { x: position.x, y: position.y },
          data: {
            title: step.title,
            status: step.status,
            statusLabel: step.status_label,
            role: step.role,
            duration: durationLabel(step.duration_ms),
            progress: step.progress,
            attention: step.needs_attention,
          },
          class: relation ? `run-graph-node run-graph-node--${relation}` : "run-graph-node",
          selected: selectedId === step.id,
          selectable: true,
          draggable: false,
          focusable: true,
          ariaLabel: `${step.title}, ${step.status_label}`,
        } satisfies Node;
      });
    edges =
      visibleGraph.edges.map((edge) => {
        const related =
          !selectedId ||
          edge.source === selectedId ||
          edge.target === selectedId ||
          (neighborhood.upstream.has(edge.source) &&
            (neighborhood.upstream.has(edge.target) || edge.target === selectedId)) ||
          (neighborhood.downstream.has(edge.target) &&
            (neighborhood.downstream.has(edge.source) || edge.source === selectedId));
        return {
          ...edge,
          type: "smoothstep",
          animated:
            related &&
            visibleGraph.nodes.some(
              (node) =>
                node.id === edge.target &&
                ["running", "queued"].includes(node.status),
            ),
          class: related ? "run-graph-edge--related" : "run-graph-edge--muted",
          style: related
            ? "stroke: var(--color-umber); stroke-width: 1.5"
            : "stroke: var(--color-rule); stroke-width: 1",
        } satisfies Edge;
      });
  }

  function applyPositions(nextPositions: RunGraphPosition[]) {
    for (const position of nextPositions) positions.set(position.id, position);
    renderGraph();
  }

  async function layout() {
    worker?.terminate();
    worker = null;
    layoutError = "";
    layingOut = true;
    if (typeof Worker !== "undefined") {
      worker = new Worker(new URL("./run-layout.worker.ts", import.meta.url), {
        type: "module",
      });
      worker.onmessage = (
        event: MessageEvent<{
          positions?: RunGraphPosition[];
          error?: string;
        }>,
      ) => {
        if (event.data.positions) applyPositions(event.data.positions);
        if (event.data.error) layoutError = event.data.error;
        layingOut = false;
        worker?.terminate();
        worker = null;
      };
      worker.onerror = () => {
        layoutError = "Automatic layout was unavailable; showing the stable fallback.";
        layingOut = false;
        worker?.terminate();
        worker = null;
      };
      worker.postMessage({ graph: displayedGraph() });
      return;
    }
    layoutError = "Automatic layout was unavailable; showing the stable fallback.";
    layingOut = false;
  }

  function layoutSignature(): string {
    const visibleGraph = displayedGraph();
    return [
      focusBranch ? selectedId ?? "" : "all",
      visibleGraph.nodes.map((node) => node.id).join(","),
      visibleGraph.edges
        .map((edge) => `${edge.source}>${edge.target}`)
        .join(","),
    ].join("|");
  }

  async function toggleFullscreen() {
    if (!container) return;
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else {
      await container.requestFullscreen();
    }
  }

  function handleFullscreen() {
    fullscreen = document.fullscreenElement === container;
  }

  $effect(() => {
    graph;
    renderGraph();
    const signature = layoutSignature();
    if (signature !== lastLayoutSignature) {
      lastLayoutSignature = signature;
      void layout();
    }
  });

  $effect(() => {
    document.addEventListener("fullscreenchange", handleFullscreen);
    return () => document.removeEventListener("fullscreenchange", handleFullscreen);
  });

  onDestroy(() => worker?.terminate());
</script>

<section class="run-graph" class:run-graph--fullscreen={fullscreen} bind:this={container}>
  <header>
    <div>
      <strong>Dependency graph</strong>
      <span>
        {displayedGraph().nodes.length} steps · {displayedGraph().edges.length} dependencies
        {#if graph.large} · layout worker{/if}
      </span>
    </div>
    <div class="graph-actions">
      <label>
        <span>Select step</span>
        <select
          value={selectedId ?? ""}
          onchange={(event) => {
            if (event.currentTarget.value) onselect(event.currentTarget.value);
          }}
        >
          <option value="">Choose a graph node</option>
          {#each graph.nodes as step (step.id)}
            <option value={step.id}>{step.title}</option>
          {/each}
        </select>
      </label>
      {#if graph.large && selectedId}
        <Button
          variant="quiet"
          size="sm"
          onclick={() => (focusBranch = !focusBranch)}
        >
          {focusBranch ? "Show all branches" : "Focus this branch"}
        </Button>
      {/if}
      <Button variant="quiet" size="sm" onclick={() => void layout()} disabled={layingOut}>
        {layingOut ? "Laying out…" : "Reset layout"}
      </Button>
      <Button variant="outline" size="sm" onclick={() => void toggleFullscreen()}>
        {fullscreen ? "Exit full screen" : "Full screen"}
      </Button>
    </div>
  </header>

  {#if layoutError}
    <p class="layout-note" role="status">{layoutError}</p>
  {/if}

  <div class="flow-stage" aria-label="Interactive workflow dependency graph">
    <SvelteFlow
      {nodes}
      {edges}
      {nodeTypes}
      fitView
      minZoom={0.18}
      maxZoom={1.8}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      onnodeclick={({ node }) => onselect(node.id)}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Lines} gap={28} size={0.6} />
      <Controls showLock={false} />
      <MiniMap pannable zoomable />
    </SvelteFlow>
  </div>
</section>

<style>
  .run-graph {
    min-width: 0;
    height: 100%;
    min-height: 36rem;
    display: grid;
    grid-template-rows: auto auto minmax(30rem, 1fr);
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .run-graph--fullscreen {
    width: 100vw;
    height: 100vh;
    border: 0;
    background: var(--color-paper);
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.72rem 0.85rem;
  }

  header > div:first-child {
    display: grid;
    gap: 0.15rem;
  }

  header strong {
    font-family: var(--font-editorial);
    font-size: 0.94rem;
    font-weight: 560;
  }

  header span {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
  }

  .graph-actions {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .graph-actions label {
    position: relative;
  }

  .graph-actions label > span {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .graph-actions select {
    width: min(15rem, 25vw);
    min-height: 2.1rem;
    border: 1px solid var(--color-rule);
    border-radius: 0;
    padding: 0.42rem 1.7rem 0.42rem 0.55rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-size: 0.61rem;
  }

  .layout-note {
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem 0.8rem;
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
    font-size: 0.64rem;
  }

  .flow-stage {
    min-height: 30rem;
    background: var(--color-surface-muted);
  }

  :global(.svelte-flow__node.run-graph-node) {
    border: 0;
    border-radius: 0;
    background: transparent;
  }

  :global(.svelte-flow__node.run-graph-node--unrelated) {
    opacity: 0.38;
  }

  :global(.svelte-flow__node.run-graph-node--upstream article) {
    border-color: var(--color-tobacco);
  }

  :global(.svelte-flow__node.run-graph-node--downstream article) {
    border-color: var(--color-lichen);
  }

  :global(.svelte-flow__controls),
  :global(.svelte-flow__minimap) {
    border: 1px solid var(--color-rule-strong);
    border-radius: 0;
    background: var(--color-surface);
    box-shadow: none;
  }

  :global(.svelte-flow__controls button) {
    border-bottom-color: var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink);
  }

  :global(.svelte-flow__background) {
    color: var(--color-rule);
  }

  @media (max-width: 44rem) {
    .run-graph {
      min-height: 31rem;
      grid-template-rows: auto auto minmax(25rem, 1fr);
    }

    header {
      align-items: flex-start;
      flex-direction: column;
    }

    .graph-actions {
      width: 100%;
      flex-wrap: wrap;
    }

    .graph-actions label {
      flex: 1 1 10rem;
    }

    .graph-actions select {
      width: 100%;
    }
  }
</style>
