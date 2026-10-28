import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendRunGraph } from "@carlina/sdk";

import {
  dependencyNeighborhood,
  fallbackRunGraphPositions,
} from "../src/run-graph.ts";

const graph: FrontendRunGraph = {
  nodes: [
    { id: "a", dependency_ids: [] },
    { id: "b", dependency_ids: ["a"] },
    { id: "c", dependency_ids: ["b"] },
    { id: "d", dependency_ids: ["a"] },
  ].map((node, index) => ({
    ...node,
    key: node.id,
    title: node.id.toUpperCase(),
    purpose: "",
    type: "task",
    status: "planned",
    status_label: "Planned",
    sequence: index,
    role: "",
    parent_id: null,
    duration_ms: null,
    progress: 0,
    needs_attention: false,
    artifact_count: 0,
    created_at: null,
    started_at: null,
    completed_at: null,
    updated_at: null,
  })),
  edges: [
    { id: "a:b", source: "a", target: "b" },
    { id: "b:c", source: "b", target: "c" },
    { id: "a:d", source: "a", target: "d" },
  ],
  large: false,
  truncated: false,
  layout_direction: "RIGHT",
};

test("finds transitive upstream and downstream dependencies", () => {
  const neighborhood = dependencyNeighborhood(graph, "b");
  assert.deepEqual([...neighborhood.upstream], ["a"]);
  assert.deepEqual([...neighborhood.downstream], ["c"]);
});

test("fallback layout is stable and respects dependency depth", () => {
  const first = fallbackRunGraphPositions(graph);
  const second = fallbackRunGraphPositions(graph);
  assert.deepEqual(first, second);
  const byId = new Map(first.map((position) => [position.id, position]));
  assert.ok(byId.get("b")!.x > byId.get("a")!.x);
  assert.ok(byId.get("c")!.x > byId.get("b")!.x);
});

test("keeps a 120-step fan-out graph uniquely positioned", () => {
  const nodes = Array.from({ length: 120 }, (_, index) => {
    const id = `step-${String(index).padStart(3, "0")}`;
    const dependencyIds =
      index === 0
        ? []
        : [
            `step-${String(Math.max(0, index - 1)).padStart(3, "0")}`,
            ...(index > 4 && index % 5 === 0
              ? [`step-${String(index - 4).padStart(3, "0")}`]
              : []),
          ];
    return {
      ...graph.nodes[0]!,
      id,
      key: id,
      title: `Synthetic step ${index + 1}`,
      sequence: index + 1,
      dependency_ids: dependencyIds,
    };
  });
  const largeGraph: FrontendRunGraph = {
    ...graph,
    nodes,
    edges: nodes.flatMap((node) =>
      node.dependency_ids.map((source) => ({
        id: `${source}:${node.id}`,
        source,
        target: node.id,
      })),
    ),
    large: true,
  };
  const positions = fallbackRunGraphPositions(largeGraph);
  assert.equal(positions.length, 120);
  assert.equal(
    new Set(positions.map((position) => `${position.x}:${position.y}`)).size,
    120,
  );
});
