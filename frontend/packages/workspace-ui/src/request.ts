import type { AddonMountContext } from "@carlina/sdk";

function addonUrl(context: AddonMountContext, path: string): URL {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("..")) {
    throw new TypeError("Addon request paths must be safe root-relative paths.");
  }
  const origin = globalThis.location?.origin ?? "http://localhost";
  return new URL(
    `/addons/${encodeURIComponent(context.addonId)}${path}`,
    origin,
  );
}

export interface RequestOptions {
  body?: BodyInit | null;
  method?: string;
  query?: Record<string, unknown>;
  signal?: AbortSignal;
}

export async function requestAddon(
  context: AddonMountContext,
  path: string,
  options: RequestOptions = {},
): Promise<unknown> {
  const url = addonUrl(context, path);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== "" && value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url, {
    body: options.body,
    credentials: "same-origin",
    headers:
      typeof options.body === "string"
        ? { "Content-Type": "application/json" }
        : undefined,
    method: options.method ?? "GET",
    signal: options.signal ?? context.signal,
  });
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message =
      typeof payload === "object" &&
      payload !== null &&
      "error" in payload
        ? String((payload as { error: unknown }).error)
        : `Request failed (${response.status}).`;
    throw new Error(message);
  }
  return payload;
}

export async function streamAddon(
  context: AddonMountContext,
  path: string,
  body: BodyInit,
  onEvent: (event: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(addonUrl(context, path), {
    body,
    credentials: "same-origin",
    headers:
      typeof body === "string"
        ? { "Content-Type": "application/json" }
        : undefined,
    method: "POST",
    signal: signal ?? context.signal,
  });
  await consumeEventStream(response, onEvent);
}

export async function streamAddonQuery(
  context: AddonMountContext,
  path: string,
  query: Record<string, unknown>,
  onEvent: (event: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = addonUrl(context, path);
  for (const [key, value] of Object.entries(query)) {
    if (value !== "" && value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: { Accept: "text/event-stream" },
    method: "GET",
    signal: signal ?? context.signal,
  });
  await consumeEventStream(response, onEvent);
}

async function consumeEventStream(
  response: Response,
  onEvent: (event: Record<string, unknown>) => void,
): Promise<void> {
  if (!response.ok) {
    const payload = await response.text();
    let message = payload || `Request failed (${response.status}).`;
    try {
      const parsed = JSON.parse(payload) as { error?: unknown };
      message =
        typeof parsed?.error === "string"
          ? parsed.error
          : `Request failed (${response.status}).`;
    } catch {
      // Preserve the response body when it is not JSON.
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error("The server returned no event stream.");

  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  const ndjson = contentType.includes("ndjson");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const emit = (data: string) => {
    if (!data.trim()) return;
    try {
      const parsed = JSON.parse(data);
      onEvent(
        typeof parsed === "object" && parsed !== null
          ? (parsed as Record<string, unknown>)
          : { message: String(parsed) },
      );
    } catch {
      onEvent({ message: data, type: "log" });
    }
  };

  const consumeSse = (flush = false) => {
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    if (flush && buffer.trim()) {
      frames.push(buffer);
      buffer = "";
    }
    for (const frame of frames) {
      const data = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      emit(data);
    }
  };

  const consumeNdjson = (flush = false) => {
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? "";
    if (flush && buffer.trim()) {
      lines.push(buffer);
      buffer = "";
    }
    for (const line of lines) emit(line);
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    if (ndjson) consumeNdjson(done);
    else consumeSse(done);
    if (done) return;
  }
}
