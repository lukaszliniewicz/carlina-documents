import type {
  FrontendAgentEventMessage,
  FrontendChatTimelineItem,
} from "@carlina/sdk";

export interface SseChunkResult {
  buffer: string;
  events: unknown[];
}

export interface ComposerAttachment {
  id: string;
  filePath: string;
  fileName: string;
  fileSize: number;
  mimeType: string;
  pass_as: string;
  source: string;
}

export interface InlineComposerSuggestion {
  id: string;
  label: string;
  description?: string;
  insertText: string;
}

export type InlineSearchRequestTarget =
  | {
      kind: "core";
      path: string;
    }
  | {
      kind: "addon";
      addonId: string;
      path: string;
    };

export function resolveInlineSearchRequestTarget(
  endpoint: string,
  applicationOrigin: string,
): InlineSearchRequestTarget {
  const origin = new URL(applicationOrigin);
  const url = new URL(endpoint, origin);
  if (url.origin !== origin.origin) {
    throw new Error("Inline search endpoints must use this application.");
  }

  const path = `${url.pathname}${url.search}`;
  if (url.pathname.startsWith("/api/")) {
    return { kind: "core", path };
  }

  const addonMatch = url.pathname.match(
    /^\/addons\/([A-Za-z0-9_.-]+)(\/api\/.*)$/,
  );
  if (!addonMatch) {
    throw new Error(
      "Inline search endpoints must target a core or addon API path.",
    );
  }
  return {
    kind: "addon",
    addonId: addonMatch[1],
    path: `${addonMatch[2]}${url.search}`,
  };
}

export interface ChatComposerConfig {
  model: string;
  thinkingLevel: string;
  assistantMode: "default" | "direct";
  capabilityIds: string[];
  runtimeProfileIds: string[];
  enableDelegation: boolean;
  runtimePolicy: "review" | "full_access";
  runtimePolicySource: "administrator" | "conversation";
  contextMode: "assisted" | "manual" | "aggressive";
  voiceModel: string;
  voice: string;
}

export interface ChatRuntimePolicyOverride {
  profile: "review" | "full_access";
}

export function chatRuntimePolicyStorageKey(sessionId: string | null): string {
  return `iarf:chat-runtime-policy:${sessionId ?? "new"}`;
}

export function parseChatRuntimePolicyOverride(
  rawValue: string | null,
): ChatRuntimePolicyOverride | null {
  if (!rawValue) return null;
  try {
    const parsed = JSON.parse(rawValue) as { profile?: unknown };
    return parsed.profile === "review" || parsed.profile === "full_access"
      ? { profile: parsed.profile }
      : null;
  } catch {
    return null;
  }
}

export function effectiveChatRuntimePolicy(
  administratorProfile: "review" | "full_access",
  override: ChatRuntimePolicyOverride | null,
): Pick<ChatComposerConfig, "runtimePolicy" | "runtimePolicySource"> {
  return override
    ? {
        runtimePolicy: override.profile,
        runtimePolicySource: "conversation",
      }
    : {
        runtimePolicy: administratorProfile,
        runtimePolicySource: "administrator",
      };
}

/**
 * Policy used to live in the global composer preference. Do not revive it:
 * administrator policy is the default, while explicit overrides belong to a
 * single conversation key.
 */
export function persistedChatComposerConfig(
  config: ChatComposerConfig,
): Omit<ChatComposerConfig, "runtimePolicy" | "runtimePolicySource"> {
  const { runtimePolicy: _runtimePolicy, runtimePolicySource: _source, ...rest } =
    config;
  return rest;
}

export function chatRuntimePolicyPayload(
  config: Pick<
    ChatComposerConfig,
    "runtimePolicy" | "runtimePolicySource" | "contextMode"
  >,
): {
  profile?: "review" | "full_access";
  context_stewardship: ChatComposerConfig["contextMode"];
} {
  return {
    ...(config.runtimePolicySource === "conversation"
      ? { profile: config.runtimePolicy }
      : {}),
    context_stewardship: config.contextMode,
  };
}

function eventKey(item: FrontendAgentEventMessage): string {
  const subjectType = String(item.event.subject?.type ?? "").trim();
  const subjectId = String(item.event.subject?.id ?? "").trim();
  if (subjectType && subjectId) {
    return `subject:${subjectType}:${subjectId}`;
  }
  return `event:${String(item.event.id ?? item.id)}`;
}

export function chatTimelineKey(item: FrontendChatTimelineItem): string {
  return item.role === "event" ? eventKey(item) : item.id;
}

function mergeEvent(
  previous: FrontendAgentEventMessage,
  next: FrontendAgentEventMessage,
): FrontendAgentEventMessage {
  return {
    ...previous,
    message_id: Math.max(previous.message_id, next.message_id),
    sequence: Math.min(previous.sequence, next.sequence),
    created_at: previous.created_at ?? next.created_at,
    event: {
      ...previous.event,
      ...next.event,
      subject: {
        ...previous.event.subject,
        ...next.event.subject,
      },
      payload: {
        ...previous.event.payload,
        ...next.event.payload,
      },
    },
  };
}

export function reconcileChatTimeline(
  source: readonly FrontendChatTimelineItem[],
): FrontendChatTimelineItem[] {
  const result: FrontendChatTimelineItem[] = [];
  const positions = new Map<string, number>();

  for (const item of source) {
    const key = chatTimelineKey(item);
    const position = positions.get(key);
    if (position === undefined) {
      positions.set(key, result.length);
      result.push(item);
      continue;
    }

    const previous = result[position];
    result[position] =
      previous.role === "event" && item.role === "event"
        ? mergeEvent(previous, item)
        : item;
  }
  return result;
}

export function mergeChatHistory(
  current: readonly FrontendChatTimelineItem[],
  incoming: readonly FrontendChatTimelineItem[],
): FrontendChatTimelineItem[] {
  return reconcileChatTimeline([...current, ...incoming]).sort((left, right) => {
    if (left.sequence !== right.sequence) {
      return left.sequence - right.sequence;
    }
    return left.message_id - right.message_id;
  });
}

export function parseSseChunk(
  previousBuffer: string,
  chunk: string,
): SseChunkResult {
  const normalized = `${previousBuffer}${chunk}`.replace(/\r\n/g, "\n");
  const frames = normalized.split("\n\n");
  const buffer = frames.pop() ?? "";
  const events: unknown[] = [];

  for (const frame of frames) {
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;
    try {
      events.push(JSON.parse(data));
    } catch {
      // A malformed event is isolated; later valid events can still reconcile.
    }
  }
  return { buffer, events };
}

export function streamPacketCommitsComposer(packetType: unknown): boolean {
  return ["turn_accepted", "final"].includes(
    String(packetType ?? ""),
  );
}

function trimmedString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function normalizeInlineSearchResults(
  payload: unknown,
): InlineComposerSuggestion[] {
  if (!Array.isArray(payload)) return [];

  const suggestions = payload.flatMap((item, itemIndex) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    if (record.is_message) return [];

    const label =
      trimmedString(record.label) ||
      trimmedString(record.name) ||
      "Unnamed result";
    const description =
      trimmedString(record.description) || trimmedString(record.email);
    const explicitInsertion =
      trimmedString(record.insertText) || trimmedString(record.insert_text);
    if (explicitInsertion) {
      return [{
        id: `${itemIndex}:${explicitInsertion}`,
        label,
        ...(description ? { description } : {}),
        insertText: explicitInsertion,
      }];
    }

    const emails = Array.isArray(record.emails)
      ? record.emails.map(trimmedString).filter(Boolean)
      : [trimmedString(record.email)].filter(Boolean);
    return emails.map((email, emailIndex) => ({
      id: `${itemIndex}:${emailIndex}:${email}`,
      label,
      description: email,
      insertText: email,
    }));
  });

  const unique = new Map<string, InlineComposerSuggestion>();
  for (const suggestion of suggestions) {
    if (!unique.has(suggestion.insertText)) {
      unique.set(suggestion.insertText, suggestion);
    }
  }
  return [...unique.values()];
}

export function chatDraftKey(sessionId: string | null): string {
  return `iarf:chat-draft:${sessionId || "new"}`;
}
