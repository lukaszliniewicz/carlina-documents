import ELK, { type ElkNode } from "elkjs/lib/elk.bundled.js";

import type { FrontendRunGraph } from "@carlina/sdk";

import type { RunGraphPosition } from "./run-graph.ts";

const elk = new ELK();

export async function layoutRunGraph(
  graph: FrontendRunGraph,
): Promise<RunGraphPosition[]> {
  const elkGraph: ElkNode = {
    id: "run",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": graph.layout_direction || "RIGHT",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.spacing.nodeNodeBetweenLayers": "90",
      "elk.spacing.nodeNode": "48",
      "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
    },
    children: graph.nodes.map((node) => ({
      id: node.id,
      width: 230,
      height: 104,
    })),
    edges: graph.edges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  };
  const result = await elk.layout(elkGraph);
  return (result.children ?? []).map((node) => ({
    id: node.id,
    x: node.x ?? 0,
    y: node.y ?? 0,
  }));
}
