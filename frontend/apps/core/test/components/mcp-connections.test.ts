import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FrontendApiClient } from "@carlina/sdk";
import McpConnectionPanel from "../../src/components/McpConnectionPanel.svelte";
import DocumentsHomePage from "../../src/routes/DocumentsHomePage.svelte";

const gateway = { enabled: true, ready: true, url: "http://127.0.0.1:5006/mcp", host: "127.0.0.1", port: 5006 };
const key = { key_id: "synthetic-key", label: "Local AI client", status: "active", scopes: [], allowed_roots: ["/tmp/Source Żółć"], created_at: null, last_used_at: null, revoked_at: null };

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function client(request: ReturnType<typeof vi.fn>): FrontendApiClient {
  return { request } as unknown as FrontendApiClient;
}

describe("MCP client setup", () => {
  it("does not issue a key when the actual listener is unavailable", async () => {
    const request = vi.fn().mockResolvedValue({ gateway: { ...gateway, enabled: false, ready: false, url: null }, scopes: [], keys: [] });
    render(McpConnectionPanel, { api: client(request) });
    await screen.findByText("Listener unavailable");
    expect(screen.queryByRole("button", { name: "Create connection" })).toBeNull();
    expect(request).toHaveBeenCalledTimes(1);
    await fireEvent.click(screen.getByRole("button", { name: "Refresh connection" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  });

  it("creates a bounded connection, hides its token, copies it on request and forgets it on revoke", async () => {
    let active = false;
    const request = vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === "POST") { active = true; return { ...key, url: gateway.url, raw_token: "synthetic-one-time-token" }; }
      if (init?.method === "DELETE") { active = false; return { key_id: key.key_id, status: "revoked" }; }
      return { gateway, scopes: [], keys: active ? [key] : [] };
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(McpConnectionPanel, { api: client(request) });
    await screen.findByText("Local MCP ready");
    await fireEvent.input(screen.getByLabelText(/Source folder/), { target: { value: " /tmp/Source Żółć " } });
    await fireEvent.click(screen.getByRole("button", { name: "Create connection" }));
    await screen.findByText("Connection created");
    const bearer = screen.getByLabelText("Bearer token") as HTMLInputElement;
    expect(bearer.type).toBe("password");
    expect(request.mock.calls.find(([, init]) => init?.method === "POST")?.[1]?.body)
      .toBe(JSON.stringify({ label: "Local AI client", allowed_roots: ["/tmp/Source Żółć"] }));
    await fireEvent.click(screen.getByRole("button", { name: "Copy token" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("synthetic-one-time-token"));
    await fireEvent.click(screen.getByRole("button", { name: "Revoke Local AI client" }));
    await waitFor(() => expect(screen.queryByLabelText("Bearer token")).toBeNull());
    expect(screen.queryByRole("region", { name: "Active MCP connections" })).toBeNull();
  });

  it("reads back a failed write without automatically issuing another key", async () => {
    let postCalls = 0;
    const request = vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === "POST") { postCalls++; throw new Error("Connection response was lost."); }
      return { gateway, scopes: [], keys: postCalls ? [key] : [] };
    });
    render(McpConnectionPanel, { api: client(request) });
    await screen.findByText("Local MCP ready");
    await fireEvent.click(screen.getByRole("button", { name: "Create connection" }));
    await screen.findByText("Connection response was lost.");
    await screen.findByRole("button", { name: "Revoke Local AI client" });
    expect(postCalls).toBe(1);
    expect(screen.queryByLabelText("Bearer token")).toBeNull();
  });

  it("clears the one-time token when setup is closed and reopened", async () => {
    const request = vi.fn(async (_path: string, init?: RequestInit) => init?.method === "POST"
      ? { ...key, url: gateway.url, raw_token: "synthetic-one-time-token" }
      : { gateway, scopes: [], keys: [key] });
    render(DocumentsHomePage, { api: client(request), bootstrap: { addons: [], feature_flags: {}, user: { id: "1", display_name: "Reader", is_admin: true } }, navigate: vi.fn() });
    await fireEvent.click(screen.getByRole("button", { name: "Connect AI client" }));
    await screen.findByText("Local MCP ready");
    await fireEvent.click(screen.getByRole("button", { name: "Create connection" }));
    await screen.findByLabelText("Bearer token");
    await fireEvent.click(screen.getByRole("button", { name: "Connect AI client" }));
    await waitFor(() => expect(screen.queryByLabelText("Bearer token")).toBeNull());
    await fireEvent.click(screen.getByRole("button", { name: "Connect AI client" }));
    await screen.findByText("Local MCP ready");
    expect(screen.queryByLabelText("Bearer token")).toBeNull();
  });

  it("keeps connection management behind the administrator role in the shell", async () => {
    const request = vi.fn();
    render(DocumentsHomePage, { api: client(request), bootstrap: { addons: [], feature_flags: {}, user: { id: "2", display_name: "Reader", is_admin: false } }, navigate: vi.fn() });
    await fireEvent.click(screen.getByRole("button", { name: "Connect AI client" }));
    expect(screen.getByText("An administrator can create an MCP connection for this account.")).toBeTruthy();
    expect(request).not.toHaveBeenCalled();
  });
});
