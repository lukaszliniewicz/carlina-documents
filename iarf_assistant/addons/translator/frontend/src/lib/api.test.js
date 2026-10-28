import { describe, expect, it, vi } from "vitest";
import { createApiClient, createIdempotencyKey } from "./api.js";

describe("translator API client", () => {
  it("normalizes structured conflict responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "Version changed", latest_segment: { id: 3 } }), { status: 409 })));
    const client = createApiClient();
    await expect(client.fetchJson("/addons/translator/api/projects/1/segments/3")).rejects.toMatchObject({ status: 409, message: "Version changed" });
    vi.unstubAllGlobals();
  });

  it("creates namespaced idempotency keys", () => {
    expect(createIdempotencyKey("translator.segment.1")).toMatch(/^translator\.segment\.1\./);
  });

  it("keeps shared tokens out of API URLs while exposing an explicit navigation URL", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = createApiClient({ shareToken: "secret-token" });
    await client.fetchJson("/addons/translator/api/projects/1");
    expect(fetchMock.mock.calls[0][0]).toBe("/addons/translator/api/projects/1");
    expect(fetchMock.mock.calls[0][1].headers["X-Shared-Token"]).toBe("secret-token");
    expect(client.tokenizedUrl("/addons/translator/api/projects/1")).toContain("token=secret-token");
    vi.unstubAllGlobals();
  });

  it("uses the dedicated capability header without putting grant secrets in URLs", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = createApiClient({ shareGrantToken: "grant-secret" });
    await client.fetchJson("/addons/translator/api/projects/1");
    expect(fetchMock.mock.calls[0][0]).toBe("/addons/translator/api/projects/1");
    expect(fetchMock.mock.calls[0][1].headers["X-Translator-Share-Token"]).toBe("grant-secret");
    expect(client.tokenizedUrl("/addons/translator/api/projects/1")).not.toContain("grant-secret");
    vi.unstubAllGlobals();
  });

  it("adds the review-session CSRF token only to unsafe requests", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = createApiClient({ shareSessionCsrf: "csrf-value" });
    await client.fetchJson("/addons/translator/api/projects/1");
    await client.fetchJson("/addons/translator/api/projects/1/blocks/2/candidate", { method: "PUT" });
    expect(fetchMock.mock.calls[0][1].headers["X-Translator-CSRF"]).toBeUndefined();
    expect(fetchMock.mock.calls[1][1].headers["X-Translator-CSRF"]).toBe("csrf-value");
    expect(fetchMock.mock.calls[1][1].credentials).toBe("same-origin");
    vi.unstubAllGlobals();
  });
});
