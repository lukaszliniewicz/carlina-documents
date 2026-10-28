import assert from "node:assert/strict";
import test from "node:test";

import {
  mergeWorkspacePage,
  resolveArtifactViewer,
  structuredRows,
  trustedArtifactAssetUrl,
  workspaceRefreshTargets,
} from "../src/lib/workspace-state.ts";

test("maps typed runtime events to the smallest inspector refresh set", () => {
  const artifact = workspaceRefreshTargets({
    kind: "artifact.created",
    subject: { type: "artifact", id: "artifact-1" },
    payload: {
      runtime_references: {
        run_id: "run-1",
        artifact_ids: ["artifact-1"],
      },
    },
  });
  assert.deepEqual([...artifact].sort(), ["artifacts", "runs", "summary"]);

  const memory = workspaceRefreshTargets({
    kind: "memory.updated",
    subject: { type: "working_memory", id: "chat-1" },
  });
  assert.deepEqual([...memory].sort(), ["memory", "summary"]);

  const context = workspaceRefreshTargets({
    kind: "context.item.archived",
    subject: { type: "context_item", id: "ctx-1" },
  });
  assert.deepEqual([...context].sort(), ["context", "summary"]);
});

test("flattens structured memory into readable bounded rows", () => {
  const rows = structuredRows(
    {
      audience_profile: {
        confidence: "high",
        independent_sources: 2,
      },
      approved: true,
      topics: ["autumn", "editorial"],
    },
    { maxRows: 20 },
  );

  assert.deepEqual(
    rows.map(({ label, value }) => [label, value]),
    [
      ["Confidence", "high"],
      ["Independent sources", "2"],
      ["Approved", "Yes"],
      ["Topics", "autumn · editorial"],
    ],
  );
  assert.ok(!rows.some((row) => row.value.includes("{")));

  const large = structuredRows(
    Object.fromEntries(
      Array.from({ length: 500 }, (_, index) => [`field_${index}`, index]),
    ),
    { maxRows: 30 },
  );
  assert.equal(large.length, 30);
  assert.equal(large.at(-1)?.label, "Additional detail");

  const exact = structuredRows({ first: 1, second: 2 }, { maxRows: 2 });
  assert.deepEqual(
    exact.map((row) => row.label),
    ["First", "Second"],
  );
});

test("selects lazy artifact viewers without trusting arbitrary URLs", () => {
  assert.equal(
    resolveArtifactViewer({
      preview_kind: "markdown",
      mime_type: "text/markdown",
      label: "brief.md",
    }).needsContent,
    true,
  );
  assert.equal(
    resolveArtifactViewer({
      preview_kind: "image",
      mime_type: "image/png",
      label: "map.png",
    }).id,
    "image",
  );
  assert.equal(
    trustedArtifactAssetUrl(
      "/api/runtime/artifacts/artifact-1/download",
    ),
    "/api/runtime/artifacts/artifact-1/download",
  );
  assert.equal(
    trustedArtifactAssetUrl("https://example.test/private"),
    null,
  );
  assert.equal(trustedArtifactAssetUrl("/uploads/1/private"), null);
});

test("merges cursor pages by stable resource id", () => {
  assert.deepEqual(
    mergeWorkspacePage(
      [
        { id: "one", value: 1 },
        { id: "two", value: 2 },
      ],
      [
        { id: "two", value: 20 },
        { id: "three", value: 3 },
      ],
    ),
    [
      { id: "one", value: 1 },
      { id: "two", value: 20 },
      { id: "three", value: 3 },
    ],
  );
});
