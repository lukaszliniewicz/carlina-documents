import assert from "node:assert/strict";
import test from "node:test";

import {
  runIdFromRoute,
  runStudioRoute,
  runTabFromRoute,
  selectedStepFromRoute,
} from "../src/lib/run-studio.ts";

test("parses direct Run Studio routes without treating the index as a run", () => {
  assert.equal(runIdFromRoute("/runs"), null);
  assert.equal(runIdFromRoute("/runs/run%20one?tab=steps"), "run one");
  assert.equal(runTabFromRoute("/runs/a?tab=timeline"), "timeline");
  assert.equal(runTabFromRoute("/runs/a?tab=unknown"), "graph");
  assert.equal(selectedStepFromRoute("/runs/a?step=step-2"), "step-2");
});

test("builds stable client routes for tabs and selected steps", () => {
  assert.equal(runStudioRoute("run one", "graph"), "/runs/run%20one");
  assert.equal(
    runStudioRoute("run one", "steps", "step-2"),
    "/runs/run%20one?tab=steps&step=step-2",
  );
});
