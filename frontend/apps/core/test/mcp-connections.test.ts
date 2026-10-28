import assert from "node:assert/strict";
import test from "node:test";
import type { FrontendApiClient } from "@carlina/sdk";
import { createMcpConnection, readMcpConnections, revokeMcpConnection } from "../src/lib/mcp-connections.ts";

test("MCP connection requests remain same-origin, uncached and owner-independent", async () => {
  const requests: { path: string; init?: RequestInit }[] = [];
  const api = { request: async (path: string, init?: RequestInit) => { requests.push({ path, init }); return {}; } } as FrontendApiClient;
  const controller = new AbortController();
  await readMcpConnections(api, controller.signal);
  await createMcpConnection(api, " Reader client ", " /tmp/Source Żółć ");
  await createMcpConnection(api, "Existing books", " ");
  await revokeMcpConnection(api, "key/with?delimiters");
  assert.equal(requests[0].init?.signal, controller.signal);
  assert.deepEqual(JSON.parse(String(requests[1].init?.body)), { label: "Reader client", allowed_roots: ["/tmp/Source Żółć"] });
  assert.deepEqual(JSON.parse(String(requests[2].init?.body)), { label: "Existing books", allowed_roots: [] });
  assert.equal(requests[3].path, "/api/mcp/access/key%2Fwith%3Fdelimiters");
  assert.equal(requests[3].init?.method, "DELETE");
  assert.ok(requests.every(({ init }) => init?.cache === "no-store"));
});
