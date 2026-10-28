import assert from "node:assert/strict";
import test from "node:test";

import { composerInsertionFromComponentResult } from "../src/lib/component-results.ts";

test("normalizes explicit composer text and direct string results", () => {
  assert.equal(
    composerInsertionFromComponentResult({ insert_text: "  selected material  " }),
    "selected material",
  );
  assert.equal(
    composerInsertionFromComponentResult("  reusable component result  "),
    "reusable component result",
  );
});

test("normalizes structured local folder selections", () => {
  assert.equal(
    composerInsertionFromComponentResult({
      path: "  C:\\Users\\user\\Documents  ",
      resource_type: "local_path",
      permissions: ["read"],
    }),
    "C:\\Users\\user\\Documents",
  );
  assert.equal(
    composerInsertionFromComponentResult({
      path: "C:\\private",
      resource_type: "unrelated",
    }),
    "",
  );
});

test("joins selected contact addresses and ignores malformed entries", () => {
  assert.equal(
    composerInsertionFromComponentResult([
      { name: "A", email: " a@example.test " },
      { name: "No address" },
      null,
      { name: "B", email: "b@example.test" },
    ]),
    "a@example.test, b@example.test",
  );
  assert.equal(composerInsertionFromComponentResult({ unknown: true }), "");
});
