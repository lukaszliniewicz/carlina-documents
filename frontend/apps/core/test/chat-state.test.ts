import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendChatTimelineItem } from "@carlina/sdk";

import {
  chatDraftKey,
  chatRuntimePolicyPayload,
  chatRuntimePolicyStorageKey,
  effectiveChatRuntimePolicy,
  mergeChatHistory,
  normalizeInlineSearchResults,
  parseSseChunk,
  parseChatRuntimePolicyOverride,
  persistedChatComposerConfig,
  reconcileChatTimeline,
  resolveInlineSearchRequestTarget,
  streamPacketCommitsComposer,
} from "../src/lib/chat-state.ts";

function event(
  id: string,
  status: string,
  sequence: number,
): FrontendChatTimelineItem {
  return {
    id,
    message_id: sequence + 1,
    sequence,
    created_at: null,
    role: "event",
    event: {
      id,
      kind: `tool_call.${status}`,
      status,
      subject: { type: "tool_call", id: "call-1" },
      payload: { [status]: true },
    },
  };
}

test("reconciles lifecycle events by their typed subject", () => {
  const items = reconcileChatTimeline([
    event("start", "running", 1),
    event("finish", "completed", 2),
  ]);

  assert.equal(items.length, 1);
  assert.equal(items[0]?.role, "event");
  if (items[0]?.role === "event") {
    assert.equal(items[0].event.status, "completed");
    assert.deepEqual(items[0].event.payload, {
      running: true,
      completed: true,
    });
  }
});

test("merges long paginated histories without duplicate messages", () => {
  const messages = Array.from({ length: 2_000 }, (_, sequence) => ({
    id: `message:${sequence + 1}`,
    message_id: sequence + 1,
    sequence,
    created_at: null,
    role: "user" as const,
    text: `Message ${sequence + 1}`,
    attachments: [],
  }));
  const merged = mergeChatHistory(
    messages.slice(1_000),
    [...messages.slice(0, 1_100), messages[1_500]!],
  );

  assert.equal(merged.length, 2_000);
  assert.equal(merged[0]?.id, "message:1");
  assert.equal(merged.at(-1)?.id, "message:2000");
});

test("parses split and multiline SSE frames while isolating malformed data", () => {
  const first = parseSseChunk(
    "",
    'data: {"type":"status",\n' + 'data: "message":"Working"}\n\nbad',
  );
  assert.deepEqual(first.events, [{ type: "status", message: "Working" }]);

  const second = parseSseChunk(
    first.buffer,
    '\n\ndata: not-json\n\ndata: {"type":"final"}\n\n',
  );
  assert.deepEqual(second.events, [{ type: "final" }]);
  assert.equal(second.buffer, "");
});

test("drafts are isolated per conversation", () => {
  assert.equal(chatDraftKey(null), "iarf:chat-draft:new");
  assert.equal(chatDraftKey("abc"), "iarf:chat-draft:abc");
});

test("keeps the administrator policy unless a conversation override is valid", () => {
  assert.deepEqual(
    effectiveChatRuntimePolicy("full_access", null),
    { runtimePolicy: "full_access", runtimePolicySource: "administrator" },
  );
  assert.deepEqual(
    effectiveChatRuntimePolicy(
      "full_access",
      parseChatRuntimePolicyOverride('{"profile":"review"}'),
    ),
    { runtimePolicy: "review", runtimePolicySource: "conversation" },
  );
  assert.equal(parseChatRuntimePolicyOverride('{"profile":"unsafe"}'), null);
  assert.equal(chatRuntimePolicyStorageKey(null), "iarf:chat-runtime-policy:new");
  assert.equal(
    chatRuntimePolicyStorageKey("chat-1"),
    "iarf:chat-runtime-policy:chat-1",
  );
});

test("does not persist legacy global runtime policy values", () => {
  const persisted = persistedChatComposerConfig({
    model: "provider/model",
    thinkingLevel: "high",
    assistantMode: "default",
    capabilityIds: [],
    runtimeProfileIds: ["python.scratch"],
    enableDelegation: true,
    runtimePolicy: "review",
    runtimePolicySource: "conversation",
    contextMode: "assisted",
    voiceModel: "",
    voice: "",
  });

  assert.equal("runtimePolicy" in persisted, false);
  assert.equal("runtimePolicySource" in persisted, false);
  assert.equal(persisted.model, "provider/model");
});

test("sends a profile only for an explicit conversation override", () => {
  assert.deepEqual(
    chatRuntimePolicyPayload({
      runtimePolicy: "full_access",
      runtimePolicySource: "administrator",
      contextMode: "assisted",
    }),
    { context_stewardship: "assisted" },
  );
  assert.deepEqual(
    chatRuntimePolicyPayload({
      runtimePolicy: "review",
      runtimePolicySource: "conversation",
      contextMode: "manual",
    }),
    { profile: "review", context_stewardship: "manual" },
  );
});

test("only durable acceptance or finalization commits the composer", () => {
  assert.equal(streamPacketCommitsComposer("status"), false);
  assert.equal(streamPacketCommitsComposer("error"), false);
  assert.equal(streamPacketCommitsComposer("assistant_message"), false);
  assert.equal(streamPacketCommitsComposer("agent_event"), false);
  assert.equal(streamPacketCommitsComposer("turn_accepted"), true);
  assert.equal(streamPacketCommitsComposer("final"), true);
});

test("normalizes inline contact and generic insertion results", () => {
  assert.deepEqual(
    normalizeInlineSearchResults([
      { name: "Ada", emails: ["ada@example.test", "work@example.test"] },
      {
        label: "Grace",
        description: "Primary address",
        insertText: "grace@example.test",
      },
      { name: "Duplicate", email: "ada@example.test" },
      { is_message: true, label: "Keep typing" },
    ]),
    [
      {
        id: "0:0:ada@example.test",
        label: "Ada",
        description: "ada@example.test",
        insertText: "ada@example.test",
      },
      {
        id: "0:1:work@example.test",
        label: "Ada",
        description: "work@example.test",
        insertText: "work@example.test",
      },
      {
        id: "1:grace@example.test",
        label: "Grace",
        description: "Primary address",
        insertText: "grace@example.test",
      },
    ],
  );
});

test("routes inline searches through the matching safe API client", () => {
  assert.deepEqual(
    resolveInlineSearchRequestTarget(
      "/api/frontend/search?q=member",
      "https://iarf.example",
    ),
    {
      kind: "core",
      path: "/api/frontend/search?q=member",
    },
  );
  assert.deepEqual(
    resolveInlineSearchRequestTarget(
      "/addons/googlecontacts/api/search?query=Peter",
      "https://iarf.example",
    ),
    {
      kind: "addon",
      addonId: "googlecontacts",
      path: "/api/search?query=Peter",
    },
  );
  assert.throws(
    () =>
      resolveInlineSearchRequestTarget(
        "https://attacker.example/api/search",
        "https://iarf.example",
      ),
    /this application/,
  );
  assert.throws(
    () =>
      resolveInlineSearchRequestTarget(
        "/addons/googlecontacts/search",
        "https://iarf.example",
      ),
    /core or addon API path/,
  );
});
