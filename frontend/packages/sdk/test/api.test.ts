import assert from "node:assert/strict";
import test from "node:test";

import { createFrontendApiClient } from "../src/api.ts";


test("API errors retain structured response details for review workflows", async () => {
  const payload = {
    error: "Dependency review required.",
    status: "dependency_review_required",
    dependency_notices: [{ id: "license-one" }],
  };
  const client = createFrontendApiClient(
    "https://carlina.test",
    async () =>
      new Response(JSON.stringify(payload), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }),
  );

  await assert.rejects(
    () => client.request("/api/addons/install", { method: "POST" }),
    (reason: unknown) => {
      const error = reason as Error & { status?: number; payload?: unknown };
      assert.equal(error.message, payload.error);
      assert.equal(error.status, 409);
      assert.deepEqual(error.payload, payload);
      return true;
    },
  );
});
