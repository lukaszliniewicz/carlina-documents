<script lang="ts">
  import { Handle, Position, type NodeProps } from "@xyflow/svelte";

  import StatusMark from "./StatusMark.svelte";

  interface RunNodeData extends Record<string, unknown> {
    title: string;
    status: string;
    statusLabel: string;
    role: string;
    duration: string;
    progress: number | null;
    attention: boolean;
  }

  type RunNode = NodeProps & { data: RunNodeData };
  let { data, selected = false }: RunNode = $props();
</script>

<Handle type="target" position={Position.Left} />
<article class:run-node--selected={selected}>
  <header>
    <StatusMark status={data.status} label={data.statusLabel} compact />
    {#if data.attention}<span class="attention" title="Needs attention">!</span>{/if}
  </header>
  <strong>{data.title}</strong>
  <footer>
    <span>{data.role || "Unassigned"}</span>
    <span>{data.duration}</span>
  </footer>
  {#if data.progress !== null}
    <div
      class="progress"
      role="progressbar"
      aria-label={`${data.title} progress`}
      aria-valuenow={data.progress}
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <i style={`width: ${data.progress}%`}></i>
    </div>
  {/if}
</article>
<Handle type="source" position={Position.Right} />

<style>
  article {
    width: 14.4rem;
    min-height: 6.4rem;
    display: grid;
    align-content: space-between;
    gap: 0.55rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.68rem 0.75rem;
    background: var(--color-surface);
    color: var(--color-ink);
    box-shadow: 0 7px 18px color-mix(in srgb, var(--color-umber) 8%, transparent);
  }

  article.run-node--selected {
    border-color: var(--color-focus);
    box-shadow: inset 3px 0 var(--color-focus);
  }

  header,
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  strong {
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.94rem;
    font-weight: 560;
    line-height: 1.18;
    text-overflow: ellipsis;
  }

  footer {
    color: var(--color-ink-muted);
    font-size: 0.57rem;
  }

  footer span:first-child {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attention {
    width: 1.15rem;
    height: 1.15rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-danger);
    color: var(--color-danger);
    font-size: 0.64rem;
    font-weight: 800;
  }

  .progress {
    height: 2px;
    overflow: hidden;
    background: var(--color-surface-sunken);
  }

  .progress i {
    display: block;
    height: 100%;
    background: var(--color-lichen);
  }

  :global(.svelte-flow__handle) {
    width: 0.5rem;
    height: 0.5rem;
    border: 1px solid var(--color-umber);
    border-radius: 0;
    background: var(--color-paper);
  }
</style>
