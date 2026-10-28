import type { FrontendRunGraph } from "@carlina/sdk";

export interface RunGraphPosition {
  id: string;
  x: number;
  y: number;
}

export interface DependencyNeighborhood {
  upstream: Set<string>;
  downstream: Set<string>;
}

export function dependencyNeighborhood(
  graph: FrontendRunGraph,
  selectedId: string | null,
): DependencyNeighborhood {
  const upstream = new Set<string>();
  const downstream = new Set<string>();
  if (!selectedId) return { upstream, downstream };

  const parents = new Map<string, string[]>();
  const children = new Map<string, string[]>();
  for (const edge of graph.edges) {
    const targets = children.get(edge.source) ?? [];
    targets.push(edge.target);
    children.set(edge.source, targets);
    const sources = parents.get(edge.target) ?? [];
    sources.push(edge.source);
    parents.set(edge.target, sources);
  }

  const visit = (
    start: string,
    adjacency: Map<string, string[]>,
    target: Set<string>,
  ) => {
    const queue = [start];
    for (let index = 0; index < queue.length; index += 1) {
      for (const next of adjacency.get(queue[index]!) ?? []) {
        if (next === selectedId || target.has(next)) continue;
        target.add(next);
        queue.push(next);
      }
    }
  };

  visit(selectedId, parents, upstream);
  visit(selectedId, children, downstream);
  return { upstream, downstream };
}

export function fallbackRunGraphPositions(
  graph: FrontendRunGraph,
): RunGraphPosition[] {
  const depth = new Map<string, number>();
  const unresolved = new Set(graph.nodes.map((node) => node.id));
  const dependencies = new Map(
    graph.nodes.map((node) => [node.id, node.dependency_ids]),
  );

  for (let pass = 0; pass <= graph.nodes.length && unresolved.size; pass += 1) {
    for (const id of [...unresolved]) {
      const parentIds = dependencies.get(id) ?? [];
      const knownParents = parentIds.filter((parent) => depth.has(parent));
      if (parentIds.length === 0 || knownParents.length === parentIds.length) {
        depth.set(
          id,
          knownParents.length
            ? Math.max(...knownParents.map((parent) => depth.get(parent)!)) + 1
            : 0,
        );
        unresolved.delete(id);
      }
    }
  }
  for (const id of unresolved) depth.set(id, 0);

  const rowsByDepth = new Map<number, string[]>();
  for (const node of graph.nodes) {
    const column = depth.get(node.id) ?? 0;
    const rows = rowsByDepth.get(column) ?? [];
    rows.push(node.id);
    rowsByDepth.set(column, rows);
  }

  return graph.nodes.map((node) => {
    const column = depth.get(node.id) ?? 0;
    const row = (rowsByDepth.get(column) ?? []).indexOf(node.id);
    return {
      id: node.id,
      x: column * 300,
      y: row * 150,
    };
  });
}
