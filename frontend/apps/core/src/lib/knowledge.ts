import type {
  FrontendKnowledgeCollection,
  FrontendKnowledgeItem,
  FrontendKnowledgeStreamEvent,
} from "@carlina/sdk";

export type KnowledgeCollectionMode = "all" | "vector" | "regular";

export interface KnowledgeCollectionFilters {
  query?: string;
  mode?: KnowledgeCollectionMode;
  source?: string;
}

export interface KnowledgeMetadataRow {
  label: string;
  value: string;
}

export interface KnowledgeSearchResult {
  metadata: string;
  text: string;
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Readonly<Record<string, unknown>>)
    : {};
}

function readableValue(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

export function knowledgeMetadataRows(
  value: unknown,
): KnowledgeMetadataRow[] {
  const metadata = record(value);
  const rows: KnowledgeMetadataRow[] = [];
  const push = (label: string, key: string) => {
    const readable = readableValue(metadata[key]);
    if (readable) rows.push({ label, value: readable });
  };
  const from = senderLabel(metadata.from);
  if (from) rows.push({ label: "From", value: from });
  for (const [label, key] of [
    ["File", "file_name"],
    ["Original file", "original_filename"],
    ["Title", "title"],
    ["Subject", "subject"],
    ["Date", "date"],
    ["Page", "page"],
    ["Section", "section"],
    ["Status", "status"],
    ["Type", "mime_type"],
    ["Language", "language"],
  ] as const) {
    push(label, key);
  }
  return rows;
}

function senderLabel(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  return value
    .slice(0, 3)
    .map((entry) => {
      if (typeof entry === "string") return entry;
      const sender = record(entry);
      return readableValue(sender.name) || readableValue(sender.email);
    })
    .filter(Boolean)
    .join(", ");
}

export function knowledgeSourceLabel(sourceType: string): string {
  const labels: Record<string, string> = {
    document_upload: "Documents",
    email: "Email",
    googledrive: "Google Drive",
  };
  return (
    labels[sourceType] ??
    sourceType.replaceAll("_", " ").replace(/\b\w/g, (letter) =>
      letter.toUpperCase()
    )
  );
}

export function knowledgeModeLabel(mode: string): string {
  if (mode === "vector") return "Semantic search";
  if (mode === "regular") return "Direct analysis";
  return mode.replaceAll("_", " ");
}

export function collectionMetadataRows(
  collection: FrontendKnowledgeCollection,
): KnowledgeMetadataRow[] {
  const metadata = record(collection.source_metadata);
  const rows: KnowledgeMetadataRow[] = [];
  const push = (label: string, value: unknown) => {
    const readable = readableValue(value);
    if (readable) rows.push({ label, value: readable });
  };

  if (collection.source_type === "document_upload") {
    push("Default extractor", readableValue(metadata.engine) || "auto");
  } else if (collection.source_type === "email") {
    push("Account", metadata.account_label || metadata.account_id);
    push("Scope", metadata.scope);
    push("From", metadata.sender);
    push("Sender role", metadata.sender_role);
    push("To", metadata.recipient);
    push("Subject contains", metadata.subject);
    push("Body contains", metadata.body);
    push("After", metadata.after_date);
    push("Before", metadata.before_date);
    if (typeof metadata.include_others === "boolean") {
      rows.push({
        label: "Thread participants",
        value: metadata.include_others
          ? "Include other participants"
          : "Only matching sender",
      });
    }
    push("Legacy query", metadata.query);
  } else if (collection.source_type === "googledrive") {
    push("Account", metadata.account_label || metadata.account_id);
    const items = Array.isArray(metadata.items) ? metadata.items : [];
    if (items.length) {
      const names = items
        .slice(0, 4)
        .map((item) => readableValue(record(item).name))
        .filter(Boolean);
      rows.push({
        label: "Drive sources",
        value: `${names.join(", ")}${items.length > names.length ? ` +${items.length - names.length}` : ""}`,
      });
    }
  }

  return rows;
}

export function knowledgeCollectionSearchText(
  collection: FrontendKnowledgeCollection,
): string {
  return [
    collection.name,
    collection.description,
    knowledgeSourceLabel(collection.source_type),
    knowledgeModeLabel(collection.collection_mode),
    ...collectionMetadataRows(collection).flatMap((row) => [
      row.label,
      row.value,
    ]),
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
}

export function filterKnowledgeCollections(
  collections: FrontendKnowledgeCollection[],
  filters: KnowledgeCollectionFilters,
): FrontendKnowledgeCollection[] {
  const needle = filters.query?.trim().toLocaleLowerCase() ?? "";
  return collections.filter((collection) => {
    if (
      filters.mode &&
      filters.mode !== "all" &&
      collection.collection_mode !== filters.mode
    ) {
      return false;
    }
    if (
      filters.source &&
      filters.source !== "all" &&
      collection.source_type !== filters.source
    ) {
      return false;
    }
    return !needle || knowledgeCollectionSearchText(collection).includes(needle);
  });
}

export function knowledgeItemTitle(item: FrontendKnowledgeItem): string {
  const metadata = record(item.metadata);
  return (
    readableValue(metadata.file_name) ||
    readableValue(metadata.original_filename) ||
    readableValue(metadata.subject) ||
    readableValue(metadata.title) ||
    item.source_id ||
    `Knowledge item ${item.id}`
  );
}

export function knowledgeItemDetails(
  item: FrontendKnowledgeItem,
): KnowledgeMetadataRow[] {
  return knowledgeMetadataRows(item.metadata).filter(
    (row) => !["File", "Original file", "Title", "Subject"].includes(row.label),
  );
}

export function parseKnowledgeSearchResult(
  value: unknown,
): KnowledgeSearchResult {
  const raw =
    typeof value === "string"
      ? value.trim()
      : readableValue(record(value).text) ||
        readableValue(record(value).content) ||
        readableValue(record(value).summary);
  const match = raw.match(
    /^### SOURCE DOCUMENT METADATA:\s*\[(.*?)\]\s*###\s*([\s\S]*)$/i,
  );
  if (!match) {
    return { metadata: "", text: raw || "No readable result content." };
  }
  return {
    metadata: match[1]?.trim() ?? "",
    text: match[2]?.trim() ?? "",
  };
}

export function knowledgeJobFinished(status: unknown): boolean {
  return ["completed", "done", "finished", "success"].includes(
    String(status || "").toLocaleLowerCase(),
  );
}

export function knowledgeJobFailed(status: unknown): boolean {
  return ["failed", "error"].includes(
    String(status || "").toLocaleLowerCase(),
  );
}

export async function consumeKnowledgeEventStream(
  response: Response,
  onEvent: (event: FrontendKnowledgeStreamEvent) => void,
): Promise<void> {
  if (!response.ok) {
    const body = await response.text();
    let message = body || `Request failed (${response.status}).`;
    try {
      const payload = JSON.parse(body) as { error?: unknown; message?: unknown };
      const detail = payload.error ?? payload.message;
      if (typeof detail === "string" && detail.trim()) message = detail;
    } catch {
      // A plain-text response is already the most useful error message.
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error("The server returned no event stream.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const emit = (frame: string) => {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data.trim()) return;
    try {
      const parsed = JSON.parse(data);
      if (typeof parsed === "object" && parsed !== null) {
        const event = parsed as Record<string, unknown>;
        onEvent({
          ...event,
          type: readableValue(event.type) || "log",
          message: readableValue(event.message) || "Knowledge activity updated.",
        });
      } else {
        onEvent({ type: "log", message: String(parsed) });
      }
    } catch {
      onEvent({ type: "log", message: data });
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    if (done && buffer.trim()) {
      frames.push(buffer);
      buffer = "";
    }
    frames.forEach(emit);
    if (done) return;
  }
}

export async function streamKnowledgeEvents(
  path: string,
  init: RequestInit,
  onEvent: (event: FrontendKnowledgeStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = new URL(path, window.location.origin);
  if (
    url.origin !== window.location.origin ||
    !url.pathname.startsWith("/api/frontend/knowledge/")
  ) {
    throw new TypeError(
      "Knowledge streams must use the same-origin frontend Knowledge API.",
    );
  }
  const response = await fetch(url, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "text/event-stream",
      ...init.headers,
    },
    signal,
  });
  await consumeKnowledgeEventStream(response, onEvent);
}
