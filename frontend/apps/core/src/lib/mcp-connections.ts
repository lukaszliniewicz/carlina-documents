import type { FrontendApiClient } from "@carlina/sdk";

export interface McpConnection {
  key_id: string;
  label: string;
  status: string;
  scopes: string[];
  allowed_roots: string[];
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface McpConnections {
  gateway: {
    enabled: boolean;
    ready: boolean;
    url: string | null;
    host: string | null;
    port: number | null;
  };
  scopes: string[];
  keys: McpConnection[];
}

export interface IssuedMcpConnection {
  key_id: string;
  raw_token: string;
  url: string;
  scopes: string[];
  allowed_roots: string[];
}

export function readMcpConnections(
  api: FrontendApiClient,
  signal?: AbortSignal,
): Promise<McpConnections> {
  return api.request<McpConnections>("/api/mcp/access", { cache: "no-store", signal });
}

export function createMcpConnection(
  api: FrontendApiClient,
  label: string,
  folder: string,
): Promise<IssuedMcpConnection> {
  return api.request<IssuedMcpConnection>("/api/mcp/access", {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      label: label.trim(),
      allowed_roots: folder.trim() ? [folder.trim()] : [],
    }),
  });
}

export function revokeMcpConnection(
  api: FrontendApiClient,
  keyId: string,
): Promise<{ key_id: string; status: "revoked" }> {
  return api.request(`/api/mcp/access/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
    cache: "no-store",
  });
}
