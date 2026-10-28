import assert from "node:assert/strict";
import test from "node:test";

import type { AddonApplicationMatch } from "@carlina/sdk";

import { routePageKey } from "../src/lib/route-page.ts";

const compassMatch = {
  addon: { id: "compass" },
  basePath: "/addons/compass",
} as AddonApplicationMatch;
const routeKeys = ["/", "/workspaces", "/knowledge", "/runs", "/settings"];

test("nested routes keep a stable addon page identity", () => {
  assert.equal(
    routePageKey("/addons/compass", compassMatch, routeKeys),
    "addon:compass:/addons/compass",
  );
  assert.equal(
    routePageKey("/addons/compass/tasks", compassMatch, routeKeys),
    routePageKey("/addons/compass/notes", compassMatch, routeKeys),
  );
});

test("core route families stay stable without conflating different pages", () => {
  assert.equal(routePageKey("/runs/123", undefined, routeKeys), "/runs");
  assert.equal(
    routePageKey("/settings/addons", undefined, routeKeys),
    "/settings",
  );
  assert.notEqual(
    routePageKey("/workspaces", undefined, routeKeys),
    routePageKey("/knowledge", undefined, routeKeys),
  );
  assert.equal(routePageKey("/missing", undefined, routeKeys), "");
});
